"""One bounded, explicitly requested Arcus open/close verification."""
import time
import uuid
from decimal import ROUND_FLOOR

from src.common.market_data import age_seconds, utc_now
from src.execution.orders import decimal, fresh_quote
from src.execution.telemetry import safe_reason


def _position(adapter, symbol):
    row = adapter.position_snapshot(symbol)
    return {key: row.get(key) for key in ('qty', 'side', 'fetched_at')}


def run_round_trip(adapter, symbol, notional, cap, hold_seconds=0,
                   check_only=False, persist=None):
    report = {'success': False, 'orders_submitted': False, 'symbol': symbol,
              'started_at': utc_now(), 'open': None, 'close': None, 'close_attempts': []}

    def stage(name):
        report['stage'] = name
        if persist:
            persist(report)

    try:
        stage('检查现有持仓与挂单')
        report['position_before'] = _position(adapter, symbol)
        if decimal(report['position_before']['qty']) != 0:
            report['error'] = 'Arcus 当前市场已有持仓，禁止测试叠加'
            return report
        if adapter._active_orders(symbol):
            report['error'] = 'Arcus 当前市场有活动订单，测试停止'
            return report
        stage('计算最小金额与数量')
        market = adapter._market(symbol)
        quote = adapter.get_executable_quote(symbol, market['min_base_amount'])
        minimum = max(decimal(market['min_quote_amount']),
                      decimal(market['min_base_amount']) * decimal(quote['ask'], True))
        target = decimal(notional) if decimal(notional) else minimum * decimal('1.05')
        cap = decimal(cap, True)
        if not minimum <= target <= cap:
            report['error'] = '测试金额不满足平台最低金额或超过已保存上限'
            return report
        step = decimal(market['step_size'], True)
        quantity = (target / decimal(quote['ask'], True) / step).to_integral_value(
            rounding=ROUND_FLOOR) * step
        if quantity < decimal(market['min_base_amount']) or quantity <= 0:
            report['error'] = '测试数量不满足平台最小数量'
            return report
        # Perform the slow account checks before fetching the final quote.
        stage('核对账户并准备连接')
        adapter.validate_order(symbol, quantity, reduce_only=False, side='buy')
        if not check_only:
            adapter.client.ws.start()
        stage('获取最终同数量报价')
        quote = adapter.get_executable_quote(symbol, quantity)
        fresh_quote(quote, symbol, quantity)
        final_notional = quantity * decimal(quote['ask'], True)
        if not minimum <= final_notional <= cap:
            report['error'] = '重新报价后测试金额越过最低金额或已保存上限'
            return report
        report.update(quantity=float(quantity), requested_notional=float(final_notional),
                      cap_usdc=float(cap), quote_age_seconds=age_seconds(quote['updated_at']))
        if check_only:
            report.update(success=True, stage='只读检查通过')
            return report
        # Unknown until the adapter has returned evidence of whether it sent.
        report['orders_submitted'] = None
        stage('提交开仓')
        opened = adapter.place_order(symbol, 'buy', quantity, quote['ask'], 10,
                                     'arcus-open-' + uuid.uuid4().hex[:16], quote,
                                     reduce_only=False)
        report['orders_submitted'] = opened.get('submitted', True)
        report['open'] = opened
        stage('核对开仓回执')
        if opened.get('confirmed') is not True:
            report.update(error='开仓回执未知，禁止重发；需核对账户', reconcile_required=True)
            return report
        filled = decimal(opened.get('filled_size', 0))
        if filled <= 0:
            report['error'] = opened.get('error') or '开仓未成交'
            return report
        remaining = filled
        report['remaining_quantity'] = float(remaining)
        if hold_seconds:
            stage('等待平仓')
            time.sleep(hold_seconds)
        for attempt in range(3):
            stage('提交只减仓平仓 ' + str(attempt + 1))
            # Account for an exception after a possible send conservatively.
            previous_sent = report['orders_submitted']
            report['orders_submitted'] = True if previous_sent is True else None
            closed = adapter.close_position(symbol, 'buy', remaining,
                                            'arcus-close-' + uuid.uuid4().hex[:16],
                                            reduce_only=True)
            report['orders_submitted'] = bool(previous_sent or closed.get('submitted', True))
            report['close'] = closed
            report['close_attempts'].append(closed)
            stage('核对平仓回执')
            if closed.get('confirmed') is not True:
                report.update(error='平仓回执未知，禁止重发；需核对账户', reconcile_required=True)
                return report
            closed_size = decimal(closed.get('filled_size', 0))
            if not 0 <= closed_size <= remaining:
                report.update(error='平仓成交数量异常，需核对账户', reconcile_required=True)
                return report
            remaining -= closed_size
            report['remaining_quantity'] = float(remaining)
            if remaining <= 0:
                break
            # Retry only confirmed terminal rejections/partial IOC fills.
            if closed.get('status') not in ('filled', 'partial', 'canceled', 'rejected'):
                report.update(error='平仓状态不可重试，需核对账户', reconcile_required=True)
                return report
        stage('核对最终持仓与挂单')
        deadline = time.monotonic() + 3
        while True:
            report['flat_after'] = _position(adapter, symbol)
            if decimal(report['flat_after']['qty']) == 0 or time.monotonic() >= deadline:
                break
            time.sleep(.25)
        report['active_orders_after'] = len(adapter._active_orders(symbol))
        report['success'] = (remaining == 0 and decimal(report['flat_after']['qty']) == 0 and
                             report['active_orders_after'] == 0)
        if not report['success']:
            report.update(error='尚未确认完全平仓，需核对账户', reconcile_required=True)
        else:
            report['stage'] = '开仓与只减仓平仓均已确认'
        return report
    except Exception as exc:
        report['error'] = safe_reason(exc)
        if report['open'] is not None or report['orders_submitted'] is not False:
            report['reconcile_required'] = True
        try:
            report['flat_after'] = _position(adapter, symbol)
        except Exception:
            pass
        return report
    finally:
        report['finished_at'] = utc_now()
        if persist:
            try:
                persist(report)
            except Exception as exc:
                # Never replace known fills with an outer "no orders" error.
                report['report_save_error'] = type(exc).__name__
