"""Bounded spread-grid settings and per-layer funding attribution."""
import math
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class GridPolicy:
    mode: str = 'single'
    step_bps: float = 5
    max_layers: int = 3
    layer_notional_usdc: float = 12
    total_notional_usdc: float = 36

    @classmethod
    def from_config(cls, config):
        values = config.get('position_policy', {}) or {}
        mode = values.get('mode', 'single')
        if mode not in ('single', 'spread_grid'):
            raise ValueError('持仓模式必须为单开或价差网格')
        result = {}
        for key, default, low, high in (
                ('step_bps', 5, .1, 10000), ('max_layers', 3, 1, 100),
                ('layer_notional_usdc', 12, 1, 1000000),
                ('total_notional_usdc', 36, 1, 1000000)):
            value = values.get(key, default)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError('价差网格配置无效：' + key)
            result[key] = value
        if int(result['max_layers']) != result['max_layers']:
            raise ValueError('网格最大层数必须为整数')
        result['max_layers'] = int(result['max_layers'])
        if mode == 'spread_grid':
            if result['total_notional_usdc'] < result['layer_notional_usdc']:
                raise ValueError('网格总金额不能小于每层金额')
            if not config.get('close_policy', {}).get('max_hold_seconds', 3600):
                raise ValueError('网格必须设置最长持仓时间')
        return cls(mode=mode, **result)


def _time(value):
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)):
        return float(value) / 1000 if float(value) > 1e11 else float(value)
    return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()


def layer_funding(snapshot, position, peers, leg):
    """Allocate each account settlement once by size at its settlement time.

    Missing timing/quantity evidence remains unknown; never give every layer
    the full account-wide amount. Accept dictionaries and live dataclasses.
    """
    def get(item, key, default=None):
        return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)
    if not get(position, 'grid_cycle_id'):
        return snapshot
    if not isinstance(snapshot, dict) or snapshot.get('rows') is None:
        return None
    allocations, total, seen = [], 0.0, set()
    try:
        for row in snapshot['rows']:
            change = float(row['change'])
            if not math.isfinite(change):
                return None
            identity = row.get('funding_id') or (row.get('timestamp'), row.get('market_id'))
            if str(identity) in seen:
                continue
            seen.add(str(identity))
            if change == 0:
                continue
            at = _time(row['timestamp'])
            eligible = [p for p in peers if get(p, 'grid_cycle_id') == get(position, 'grid_cycle_id') and
                        _time(get(p, 'open_time')) <= at and
                        (not get(p, 'closed_time') or at < _time(get(p, 'closed_time'))) and
                        (not get(p, 'close_started_time') or at < _time(get(p, 'close_started_time')))]
            quantities = {get(p, 'id'): float(get(p, 'entry_size_' + leg, 0) or 0) for p in eligible}
            quantity = sum(quantities.values())
            if quantity <= 0 or abs(abs(float(row['position_size'])) - quantity) > max(1e-8, quantity * 1e-6):
                return None
            amount = change * quantities.get(get(position, 'id'), 0) / quantity
            total += amount
            allocations.append(dict(row, account_change=change, change=amount))
        return dict(snapshot, funding_usdc=total, rows=allocations, allocation='layer_quantity_at_settlement')
    except (ValueError, TypeError, KeyError, OverflowError):
        return None
