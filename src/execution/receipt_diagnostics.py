"""Small, non-sensitive order evidence; no exchange requests or persistence."""
import math


DIAGNOSTIC_FIELDS = ('raw_status', 'time_in_force', 'limit_price',
                     'platform_reason_code', 'platform_reason', 'platform_reason_reported')
STATUS_CODES = frozenset(('FILLED', 'PARTIALLY_FILLED', 'PARTIAL', 'CANCELED',
                         'CANCELLED', 'MARGIN_CANCELED', 'REJECTED', 'EXPIRED',
                         'OPEN', 'NEW', 'PENDING', 'ACCEPTED'))
REASON_CODES = {
    'FILL_WILL_EXCEED_TRADING_BOUND': '成交将触及盘外交易价格边界',
    'INSUFFICIENT_MARGIN': '保证金不足',
    'INSUFFICIENT_BALANCE': '可用余额不足',
    'MARGIN_CANCELED': '保证金条件导致取消',
    'IOC_NO_MATCH': 'IOC 未匹配到成交',
    'IOC_UNFILLED': 'IOC 未成交部分取消',
    'IOC_EXPIRED': 'IOC 有效期结束',
    'FOK_NOT_FILLED': 'FOK 未能全部成交',
    'POST_ONLY_WOULD_TAKE': '仅挂单指令会立即吃单',
    'REDUCE_ONLY': '只减仓条件未满足',
    'SELF_TRADE': '自成交保护',
    'SELF_TRADE_PREVENTION': '自成交保护',
    'ORDER_EXPIRED': '订单有效期结束',
    'USER_CANCELED': '用户撤单',
    'USER_CANCELLED': '用户撤单',
}


def order_diagnostics(row):
    """Allowlist diagnostic fields; never retain arbitrary server messages."""
    def first(*keys):
        return next((row[k] for k in keys if row.get(k) is not None), None)

    status = str(first('raw_status', 'status', 'orderStatus', 'state') or '').upper()
    tif = first('time_in_force', 'timeInForce', 'tif')
    tif = {0: 'GTT', 1: 'FOK', 2: 'IOC', 3: 'ALO'}.get(tif, tif) if type(tif) in (int, str) else None
    tif = str(tif).upper() if tif is not None else None
    price = first('limit_price', 'price')
    try:
        price = float(price) if not isinstance(price, bool) else None
        if price is None or not math.isfinite(price) or price <= 0:
            price = None
    except (ValueError, TypeError, OverflowError):
        price = None
    reason = first('platform_reason_code', 'cancelReason', 'cancellationReason',
                   'rejectReason', 'rejectionReason', 'reasonCode', 'cancelReasonCode',
                   'rejectReasonCode', 'reason')
    reported = row.get('platform_reason_reported') is True or reason not in (None, '')
    code = str(reason or '').strip().upper()
    known = code if code in REASON_CODES else None
    return dict(raw_status=status if status in STATUS_CODES else None,
                time_in_force=tif if tif in ('IOC', 'FOK', 'GTT', 'ALO') else None,
                limit_price=price, platform_reason_code=known,
                platform_reason=REASON_CODES[known] if known else (
                    '平台返回原因未识别，原文未展示' if reported else None),
                platform_reason_reported=reported)
