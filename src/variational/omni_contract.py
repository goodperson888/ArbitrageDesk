"""Actual Omni response contract observed locally on 2026-09-27.

Pure parsing/correlation; this module never sends a request or clicks a control.
HTTP success, a cleared order, and a historical trade alone do not prove a fill
for the armed intent. Unknown fee/funding fields remain unknown.
"""
import hashlib
from decimal import Decimal
from src.execution.orders import decimal, direction
from src.common.market_data import age_seconds

QUOTE = '/api/quotes/indicative'
SUBMITS = {'/api/orders/new/market', '/api/quotes/accept'}
ORDERS = '/api/orders/v2'
TRADES = '/api/trades'
POSITIONS = '/api/positions'
BALANCE = '/api/settlement_pools/details'
ENDPOINTS = {QUOTE, ORDERS, TRADES, POSITIONS, BALANCE} | SUBMITS


def identity(row):
    """Account identifiers are represented by stable local fingerprints."""
    values = [row.get('company'), row.get('pool_location')]
    if not all(isinstance(value, str) and value for value in values):
        return None
    return ':'.join(hashlib.sha256(value.encode()).hexdigest()[:16] for value in values)


def collection(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('result'), list):
        raise ValueError('缺少分页集合')
    pagination = payload.get('pagination')
    if not isinstance(pagination, dict) or 'next_page' not in pagination:
        raise ValueError('缺少分页完整性信息')
    page = pagination.get('last_page')
    offset = page.get('offset') if isinstance(page, dict) else None
    first_page = (type(offset) is int and offset == 0) or (type(offset) is str and offset == '0')
    complete = (first_page and
                pagination['next_page'] is None and
                type(pagination.get('object_count')) is int and
                pagination['object_count'] == len(payload['result']))
    return payload['result'], complete


def market(row):
    instrument = row.get('instrument')
    if not isinstance(instrument, dict) or instrument.get('instrument_type') != 'perpetual_future':
        raise ValueError('不是已识别的永续合约')
    if instrument.get('underlying') not in ('BTC', 'ETH'):
        raise ValueError('存在未支持市场，不能推断完整账户状态')
    return instrument['underlying']


FEE_POLICY_URL = 'https://docs.variational.io/omni/trading/fees'
FEE_POLICY_MAX_AGE_SECONDS = 30 * 86400


def zero_trade_fee_policy(policy):
    age = age_seconds((policy or {}).get('verified_at'))
    return bool(policy and policy.get('source_url') == FEE_POLICY_URL and
                type(policy.get('fee_bps')) in (int, float) and policy['fee_bps'] == 0 and
                policy.get('scope') == 'confirmed_taker_trade' and
                age is not None and 0 <= age <= FEE_POLICY_MAX_AGE_SECONDS)


class OmniContract:
    def __init__(self, account_id, fee_policy=None):
        self.account_id = account_id
        self.fee_policy = fee_policy
        self.quotes = {}
        self.snapshots = {}
        self.observations = {}
        self.expected = None
        self.rfq_id = None
        self.receipt = None
        self.error = None
        self.armed_at = None
        self.last_orders, self.last_trades = [], []

    def _belongs(self, row):
        if not self.account_id or identity(row) != self.account_id:
            raise ValueError('账户/资金池身份不匹配')

    def arm(self, quote, side, size, reduce_only, at):
        if self.expected is not None and self.receipt is None:
            raise ValueError('前一提交尚无终态，禁止重新绑定')
        if not quote.get('quote_id') or decimal(quote['size'], True) != decimal(size, True):
            raise ValueError('提交意图与报价数量不符')
        if quote.get('account_id') != self.account_id:
            raise ValueError('报价账户不符')
        self.expected = dict(quote_id=quote['quote_id'], symbol=quote['symbol'], side=direction(side),
                             size=decimal(size, True), reduce_only=reduce_only,
                             max_slippage_bps=decimal(quote['max_slippage_bps']),
                             quoted_zero_fee=(type(quote.get('fee_bps')) in (int,float) and quote['fee_bps'] == 0))
        if not 0 <= self.expected['max_slippage_bps'] <= 100:
            raise ValueError('滑点限制无效')
        self.armed_at = at
        self.rfq_id = self.receipt = self.error = None
        self.last_orders, self.last_trades = [], []

    def ingest(self, endpoint, request, payload, at):
        age = age_seconds(at)
        if endpoint not in ENDPOINTS or age is None or not 0 <= age <= 10:
            return
        try:
            self._ingest(endpoint, request, payload, at)
            self.observations[endpoint] = {'at': at, 'parsed': True}
        except (ValueError, TypeError, KeyError, ArithmeticError):
            self.observations[endpoint] = {'at': at, 'parsed': False}
            # Invalid latest data must invalidate earlier success, not leave it usable.
            if endpoint == QUOTE:
                self.quotes.clear()
            if endpoint in (ORDERS, POSITIONS, BALANCE):
                self.snapshots.pop(endpoint, None)
            if self.expected and endpoint in SUBMITS | {ORDERS, TRADES}:
                self.error = '官网回执字段、身份或数量不符，停止并核对账户'
                self.receipt = None

    def _ingest(self, endpoint, request, payload, at):
        if endpoint == QUOTE:
            symbol = market(request)
            if market(payload) != symbol or decimal(payload['qty'], True) != decimal(request['qty'], True):
                raise ValueError('报价市场或数量不符')
            if not isinstance(payload.get('quote_id'), str) or not payload['quote_id']:
                raise ValueError('缺少报价编号')
            quote_age = age_seconds(payload.get('timestamp'))
            if quote_age is None or not 0 <= quote_age <= 5:
                raise ValueError('缺少新鲜的服务器报价时间')
            bid, ask = decimal(payload['bid'], True), decimal(payload['ask'], True)
            if bid > ask:
                raise ValueError('报价倒置')
            limits = {}
            for side in ('bid', 'ask'):
                values = payload['qty_limits'][side]
                tick, low, high = (decimal(values[key], True) for key in ('min_qty_tick', 'min_qty', 'max_qty'))
                size = decimal(payload['qty'], True)
                if low > high or not low <= size <= high or size % tick:
                    raise ValueError('报价数量不符合双向交易限制')
                limits[side] = {key: str(value) for key, value in zip(('tick', 'min', 'max'), (tick, low, high))}
            margin = payload['margin_requirements']
            bid_fee, ask_fee = decimal(margin['estimated_fees_bid']), decimal(margin['estimated_fees_ask'])
            # Zero needs no undocumented conversion of units. Nonzero stays blocked.
            fee_bps = 0 if bid_fee == ask_fee == 0 else None
            self.quotes[symbol] = dict(account_id=self.account_id, symbol=symbol,
                size=str(decimal(payload['qty'])), quote_id=payload['quote_id'], bid=str(bid), ask=str(ask),
                mark_price=str(decimal(payload['mark_price'], True)), fee_bps=fee_bps,
                instrument_type='perpetual', updated_at=payload['timestamp'], observed_at=at,
                qty_limits=limits, executable=False)
            return
        if endpoint == POSITIONS:
            if not isinstance(payload, list):
                raise ValueError('持仓响应必须为数组')
            positions, funding = {}, {}
            for row in payload:
                info = row['position_info']
                self._belongs(info)
                symbol = market(info)
                if symbol in positions:
                    raise ValueError('同一市场出现多条仓位，需核对资金池')
                positions[symbol] = str(decimal(info['qty']))
                try:
                    funding[symbol] = str(decimal(row['cum_funding']))
                except (ValueError, TypeError, KeyError, ArithmeticError):
                    funding[symbol] = None
            self.snapshots[endpoint] = dict(at=at, positions=positions, cumulative_funding=funding)
            return
        if endpoint == BALANCE:
            # Withdrawing limit alone does not establish an authenticated account.
            # validate_account also requires fresh, identity-bound orders and positions.
            balance = decimal(payload['max_withdrawable_amount'])
            if balance < 0:
                raise ValueError('可用余额无效')
            self.snapshots[endpoint] = dict(at=at, available_balance=str(balance))
            return
        if endpoint in SUBMITS:
            expected = self.expected
            if not expected or at < self.armed_at:
                return
            if request.get('quote_id') != expected['quote_id']:
                raise ValueError('出现并非本意图的提交')
            if request.get('side') != expected['side'] or request.get('is_reduce_only') is not expected['reduce_only']:
                raise ValueError('提交方向/只减仓不符')
            if not 0 <= decimal(request['max_slippage']) * 10000 <= expected['max_slippage_bps']:
                raise ValueError('提交滑点高于计划限制')
            rfq_id = payload['rfq_id']
            if not isinstance(rfq_id, str) or not rfq_id or (self.rfq_id and self.rfq_id != rfq_id):
                raise ValueError('提交编号缺失或发生重复提交')
            if payload.get('take_profit_rfq_id') or payload.get('stop_loss_rfq_id'):
                raise ValueError('出现计划外止盈止损订单')
            self.rfq_id = rfq_id
        elif endpoint in (ORDERS, TRADES):
            rows, complete = collection(payload)
            for row in rows:
                self._belongs(row)
            if endpoint == ORDERS:
                if not complete or not rows:
                    self.snapshots.pop(ORDERS, None)
                else:
                    # Only observed cleared status is terminal here. Other states
                    # conservatively block a new entry until explicitly mapped.
                    pending = sum(row.get('status') != 'cleared' or
                                  row.get('clearing_status') != 'success_trades_booked_into_pool' for row in rows)
                    self.snapshots[ORDERS] = dict(at=at, pending_orders=pending)
            if self.expected and at >= self.armed_at:
                if endpoint == ORDERS:
                    self.last_orders = rows
                else:
                    self.last_trades = rows
        self._resolve()

    def _resolve(self):
        if not self.expected or not self.rfq_id or self.error:
            return
        e = self.expected
        orders = [row for row in self.last_orders if row.get('rfq_id') == self.rfq_id]
        if not orders:
            return
        if len(orders) != 1:
            raise ValueError('同一 RFQ 存在多个订单')
        order = orders[0]
        if market(order) != e['symbol'] or order.get('side') != e['side'] or order.get('is_reduce_only') is not e['reduce_only']:
            raise ValueError('订单意图不匹配')
        if decimal(order['qty'], True) != e['size'] or not order.get('order_id') or order.get('tif') != 'fill_or_kill':
            raise ValueError('订单数量、编号或成交方式不符')
        if order.get('status') != 'cleared' or order.get('clearing_status') != 'success_trades_booked_into_pool':
            return  # Do not guess a rejection/partial terminal from an unknown status.
        trades = [row for row in self.last_trades if row.get('source_rfq') == self.rfq_id]
        if not trades:
            return
        seen, quantity, notional = set(), Decimal(0), Decimal(0)
        for trade in trades:
            # Live round-trip 2026-09-27: source_quote is a different venue
            # identifier from submitted indicative.quote_id. Correlate through
            # the captured submit response's rfq_id, then bind identity/size/side.
            if not isinstance(trade.get('source_quote'), str) or not trade['source_quote'] or market(trade) != e['symbol'] or trade.get('side') != e['side']:
                raise ValueError('成交关联不匹配')
            if trade.get('status') != 'confirmed':
                return
            if trade.get('role') != 'taker' or trade.get('trade_type') != 'trade':
                raise ValueError('不是本次普通 taker 成交')
            if not trade.get('id') or trade['id'] in seen:
                raise ValueError('成交编号缺失或重复')
            seen.add(trade['id'])
            size, price = decimal(trade['qty'], True), decimal(trade['price'], True)
            quantity += size
            notional += size * price
        if quantity > e['size']:
            raise ValueError('成交量超过意图')
        if quantity < e['size']:
            return  # FOK cannot be declared partially terminal from a truncated page.
        average = notional / quantity
        if average != decimal(order['price'], True):
            raise ValueError('订单成交价与交易明细不一致')
        zero_fee = e['quoted_zero_fee'] and zero_trade_fee_policy(self.fee_policy)
        self.receipt = dict(account_id=self.account_id, symbol=e['symbol'], side=e['side'],
            quote_id=e['quote_id'], reduce_only=e['reduce_only'], requested_size=str(e['size']),
            order_id=order['order_id'], status='filled', confirmed=True, filled_size=str(quantity),
            average_price=str(average), fees_usdc='0' if zero_fee else None, funding_usdc=None,
            fee_basis='official_zero_trade_fee_policy_and_bound_quote' if zero_fee else None,
            fee_policy_url=FEE_POLICY_URL if zero_fee else None,
            venue_source_quotes=sorted({t['source_quote'] for t in trades}), correlation='submit_response_rfq')

    def account(self):
        parts = [self.snapshots.get(path) for path in (ORDERS, POSITIONS, BALANCE)]
        if any(part is None or age_seconds(part['at']) is None or not 0 <= age_seconds(part['at']) <= 5 for part in parts):
            raise ValueError('账户订单、持仓或余额快照缺失/过期')
        return {'account_id': self.account_id, 'observed_at': min(p['at'] for p in parts),
                'pending_orders': parts[0]['pending_orders'], 'positions': parts[1]['positions'],
                'available_balance': parts[2]['available_balance']}

    def account_diagnostics(self):
        result = {}
        for path in (ORDERS, POSITIONS, BALANCE):
            seen = self.observations.get(path)
            snapshot = self.snapshots.get(path)
            age = age_seconds(snapshot['at']) if snapshot else None
            state = ('missing' if not seen else 'invalid_schema_or_identity' if not seen['parsed']
                     else 'incomplete_or_unbound' if not snapshot else
                     'fresh' if age is not None and 0 <= age <= 5 else 'stale')
            result[path] = {'state': state, 'age_seconds': age}
        return result
