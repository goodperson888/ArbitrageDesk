"""Bind a short-lived resume proof to the exact persisted hedge."""
import hashlib
import json

from src.common.market_data import age_seconds
from src.execution.orders import decimal
from src.execution.close_recovery import validate_resume_closes


RESUME_MAX_AGE_SECONDS = 60
RESUME_FIELDS = ('id', 'symbol', 'status', 'variational_venue', 'lighter_venue',
                 'variational_side', 'lighter_side', 'variational_size', 'lighter_size',
                 'entry_size_variational', 'entry_size_lighter',
                 'entry_price_variational', 'entry_price_lighter', 'open_time',
                 'grid_cycle_id', 'grid_layer', 'grid_anchor_bps', 'grid_entry_net_bps',
                 'close_started_time', 'close_reason', 'close_attempts',
                 'close_order_results', 'close_receipt_order_ids')


def position_fingerprint(positions):
    rows = []
    for position in positions:
        if position.get('status') == 'closed':
            continue
        row = {key: position.get(key) for key in RESUME_FIELDS}
        for key in ('open_time', 'close_started_time'):
            if hasattr(row[key], 'isoformat'):
                row[key] = row[key].isoformat()
        rows.append(row)
    rows.sort(key=lambda row: str(row['id']))
    encoded = json.dumps(rows, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def validate_paired_positions(positions, venues):
    """Only complete, opposite, equally sized legs can resume normal monitoring."""
    active = [p for p in positions if p.get('status') != 'closed']
    if not active or len(venues) != 2:
        raise ValueError('没有可接管的完整双腿持仓')
    ids = set()
    for p in active:
        if not p.get('id') or p['id'] in ids or p.get('status') not in ('open', 'closing', 'reconcile_required'):
            raise ValueError('原持仓编号或状态无法核对')
        ids.add(p['id'])
        recorded = (p.get('variational_venue'), p.get('lighter_venue'))
        if recorded != tuple(venues):
            raise ValueError('原持仓平台与本次执行平台不匹配')
        a, b = decimal(p.get('variational_size'), True), decimal(p.get('lighter_size'), True)
        if abs(a - b) > max(decimal('0.00000001'), max(a, b) * decimal('0.000001')):
            raise ValueError('原持仓双腿数量不匹配，保留待核对')
        if (p.get('variational_side'), p.get('lighter_side')) not in (('long', 'sell'), ('short', 'buy')):
            raise ValueError('原持仓双腿方向不匹配，保留待核对')
        for key in ('entry_price_variational', 'entry_price_lighter'):
            decimal(p.get(key), True)
    return active


def resume_proof_valid(state, venues):
    try:
        proof = state.get('resume_proof') or {}
        age = age_seconds(proof.get('checked_at'))
        portfolio = proof.get('portfolio') is True
        if (state.get('resume_verified') is not True or state.get('halted_reason') or
                (not portfolio and proof.get('venues') != list(venues)) or age is None or
                not 0 <= age <= RESUME_MAX_AGE_SECONDS):
            return False
        if portfolio:
            managed = (state.get('runtime_scope') or {}).get('managed_venues')
            if (not isinstance(managed, list) or proof.get('venues') != managed or
                    not set(managed) <= {'lighter', 'lighter_rh', 'arcus'} or
                    len(venues) != 2 or not set(venues) <= set(managed)):
                return False
            validate_portfolio_positions(state.get('positions', []), managed)
        else:
            validate_paired_positions(state.get('positions', []), venues)
        validate_resume_closes(state.get('positions', []))
        return proof.get('fingerprint') == position_fingerprint(state['positions'])
    except (ValueError, TypeError, ArithmeticError, AttributeError):
        return False


def validate_portfolio_positions(positions, venues):
    active = [p for p in positions if p.get('status') != 'closed']
    if not active or len({p.get('id') for p in active}) != len(active):
        raise ValueError('没有可核对的唯一持仓记录')
    for position in active:
        pair = [position.get('variational_venue'), position.get('lighter_venue')]
        if not set(pair) <= set(venues) or len(set(pair)) != 2:
            raise ValueError('原持仓平台不在已记录的管理范围')
        validate_paired_positions([position], pair)
    return active


def validate_portfolio_accounts(positions, venues, snapshots, matching_position):
    """Compare every venue+market aggregate; unrelated exposure is a blocker."""
    active = validate_portfolio_positions(positions, venues)
    groups = {}
    for position in active:
        for leg, venue_key in (('variational', 'variational_venue'), ('lighter', 'lighter_venue')):
            venue = position[venue_key]
            groups.setdefault((venue, position['symbol']), []).append((position, leg))
    for venue in venues:
        nonzero = [row for row in snapshots[venue]['positions'] if decimal(row['position']) != 0]
        if len(nonzero) != sum(v == venue for v, _ in groups):
            raise ValueError('账户持仓与已记录各组持仓不匹配')
    for (venue, _), group in groups.items():
        rows = matching_position(venue, group[0][0])
        expected = sum((decimal(p[leg+'_size']) *
                        (1 if p[leg+'_side'] in ('buy', 'long') else -1) for p,leg in group), decimal('0'))
        if not expected or any((p[leg+'_side'] in ('buy', 'long')) != (expected > 0) for p,leg in group):
            raise ValueError('同平台同品种持仓方向不一致')
        if len(rows) != 1:
            raise ValueError('账户持仓市场未匹配')
        row = rows[0]
        actual = abs(decimal(row['position'])) * int(row.get('sign', 1 if decimal(row['position']) > 0 else -1))
        if abs(actual - expected) > max(decimal('0.00000001'), abs(expected)*decimal('0.000001')):
            raise ValueError('账户持仓方向或数量不匹配')
