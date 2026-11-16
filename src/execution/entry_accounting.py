"""Match immutable entry fills locally; never query accounts or submit orders."""
import json
from dataclasses import asdict
from pathlib import Path

from src.execution.orders import decimal


ACTUAL_FEE_SOURCES = {'arcus_fills', 'arcus_order', 'lighter_trade_zero_fees', 'verified_order_receipt'}


def entry_evidence(position, orders):
    """Require venue, identity, side and the complete original quantity."""
    result = {}
    for leg in ('variational', 'lighter'):
        venue = position.get(leg + '_venue')
        raw_side = position.get(leg + '_side')
        if not venue or raw_side not in ('long', 'short', 'buy', 'sell'):
            continue
        side = 'buy' if raw_side in ('long', 'buy') else 'sell'
        try:
            quantity = decimal(position.get('entry_size_' + leg), True)
        except (ValueError, TypeError, ArithmeticError):
            continue
        identities = set(map(str, (position.get('entry_order_ids_by_venue') or {}).get(venue, [])))
        prefix = str(position['id']) + '-'
        matches, seen = [], set()
        try:
            for row in orders:
                if (row.get('venue') != venue or row.get('symbol') != position['symbol'] or
                        row.get('side') != side or row.get('reduce_only') is not False or
                        row.get('status') not in ('filled', 'canceled', 'cancelled', 'expired') or
                        row.get('source') == 'confirmed_order_receipt' or
                        row.get('price_verified') is False):
                    continue
                identity = str(row.get('order_id') or '')
                if not identity or not (identity in identities or
                        str(row.get('client_order_id') or '').startswith(prefix)):
                    continue
                if identity in seen:
                    return {}  # A duplicate cannot prove a full fill quantity.
                seen.add(identity)
                matches.append((row, decimal(row['filled_size'], True), decimal(row['average_price'], True)))
            if not matches or sum(item[1] for item in matches) != quantity:
                continue
            price = sum(size * price for _, size, price in matches) / quantity
            fees = None
            if all(row.get('fee_source') in ACTUAL_FEE_SOURCES and
                   row.get('fee_estimated') is not True and row.get('fees_usdc') is not None
                   for row, _, _ in matches):
                fees = sum(decimal(row['fees_usdc']) for row, _, _ in matches)
            result[leg] = {'price': float(price), 'fees_usdc': float(fees) if fees is not None else None,
                           'venue': venue, 'order_ids': sorted(seen), 'source': 'platform_order_history'}
        except (KeyError, ValueError, TypeError, ArithmeticError):
            continue
    return result


def read_entry_evidence(position, data_dir, history):
    """Background/local-file work, separate from quote and submission paths."""
    data = json.loads((Path(data_dir) / 'platform-order-history.json').read_text())
    if not isinstance(data, dict) or not isinstance(data.get('rows'), list):
        return {}
    row = asdict(position)
    receipts = history.receipt_orders([position.id]).get(position.id, {})
    row['entry_order_ids_by_venue'] = receipts.get('entry_by_venue', {})
    return entry_evidence(row, data.get('rows', []))


def apply_entry_evidence(position, evidence):
    """Only change the cost of a full open hedge, never exposure or targets."""
    if not evidence or position.status != 'open' or position.close_order_results or any(
            abs(getattr(position, leg + '_size') - getattr(position, 'entry_size_' + leg)) > 1e-8
            for leg in ('variational', 'lighter')):
        return False
    before = asdict(position)
    for leg, item in evidence.items():
        if leg in ('variational', 'lighter') and item['venue'] == getattr(position, leg + '_venue'):
            setattr(position, 'entry_price_' + leg, item['price'])
    if set(evidence) == {'variational', 'lighter'} and all(
            item['fees_usdc'] is not None for item in evidence.values()):
        fee = sum(item['fees_usdc'] for item in evidence.values())
        delta = position.entry_fees_usdc - fee
        position.entry_fees_usdc = fee
        if position.realized_pnl_usdc is not None:
            position.realized_pnl_usdc += delta
        position.pending_fee_venues = []
    position.entry_accounting = {**position.entry_accounting, **evidence}
    a, b = position.entry_price_variational, position.entry_price_lighter
    position.actual_entry_spread_bps = (a - b) * (1 if position.variational_side == 'short' else -1) / max(a, b) * 10000
    if position.entry_target_projection and position.entry_size_variational > 0:
        gross = (a - b) * (1 if position.variational_side == 'short' else -1)
        position.target_entry_net_bps = (gross + position.entry_target_projection['exit_credit_per_unit'] -
            position.entry_fees_usdc / position.entry_size_variational) / max(a, b) * 10000
    return before != asdict(position)
