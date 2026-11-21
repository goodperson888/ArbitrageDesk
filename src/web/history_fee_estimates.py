"""Display-only fee estimates; never promote rates to historical ledger facts."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path

from dotenv import dotenv_values


def verified_lighter_rate(profile_path, credentials_path):
    """Reuse locally bound rate evidence, with no exchange requests.

    Historical estimates use the last verification time, even after the live
    execution proof expires. They do not claim that rate applied at fill time.
    """
    try:
        profile = json.loads(Path(profile_path).read_text())
        if not isinstance(profile, dict) or not isinstance(profile.get('lighter_fee_checked_at'), str):
            return {}
        values = dotenv_values(credentials_path, interpolate=False)
        key = str(values.get('LIGHTER_API_PRIVATE_KEY') or '').removeprefix('0x')
        rate = profile.get('lighter_fee_bps')
        checked = datetime.fromisoformat(profile['lighter_fee_checked_at'].replace('Z', '+00:00'))
        bound = (key and str(profile.get('lighter_fee_account_index')) == values.get('LIGHTER_ACCOUNT_INDEX')
                 and str(profile.get('lighter_fee_key_index')) == values.get('LIGHTER_API_KEY_INDEX')
                 and profile.get('lighter_fee_key_digest') == hashlib.sha256(key.encode()).hexdigest())
        if (profile.get('lighter_fee_verified') is not True or not bound or
                type(rate) not in (int, float) or not Decimal(str(rate)).is_finite() or
                not 0 <= rate <= 10000 or checked.tzinfo is None or checked > datetime.now(timezone.utc)):
            return {}
        return {'lighter': {'bps': rate, 'checked_at': checked.isoformat()}}
    except (OSError, ValueError, TypeError, KeyError, InvalidOperation):
        return {}


def apply_fee_estimates(book, rates):
    """Add a provisional net only when matched fills and funding are complete."""
    fields = ('provisional_pnl_usdc', 'provisional_fees_usdc', 'fee_estimates',
              'provisional_accounting_note')
    for row in book.get('closed_positions', []):
        for field in fields:
            row.pop(field, None)
        if not row.get('accounting_pending') or row.get('pending_funding_venues'):
            continue
        recovered = row.get('recovered_close_fills') or {}
        known, estimated, estimates = Decimal(0), Decimal(0), []
        valid = True
        for leg in ('variational', 'lighter'):
            if not row.get('entry_size_' + leg):
                continue
            fill = recovered.get(leg) or {}
            components = fill.get('fee_components') or []
            # A cost-basis mismatch cannot be repaired by assuming a fee rate.
            if (not fill.get('terminal') or not components or
                    any('成本基准' in note for note in fill.get('missing_fees', []))):
                valid = False
                break
            for item in components:
                if item.get('fees_usdc') is not None:
                    known += Decimal(str(item['fees_usdc']))
                    continue
                venue = row.get(leg + '_venue')
                evidence = rates.get(venue)
                if not evidence:
                    valid = False
                    break
                notional = Decimal(item['notional_usdc'])
                fee = notional * Decimal(str(evidence['bps'])) / 10000
                estimated += fee
                estimates.append(dict(venue=venue, order_id=item['order_id'],
                    notional_usdc=float(notional), fee_bps=evidence['bps'],
                    fees_usdc=float(fee), checked_at=evidence['checked_at']))
            if not valid:
                break
        if not valid or not estimates or row.get('gross_price_pnl_usdc') is None:
            continue
        gross = Decimal(str(row['gross_price_pnl_usdc']))
        funding = Decimal(str(row.get('known_funding_usdc') or 0))
        fees = known + estimated
        net = gross - fees + funding
        detail = '；'.join(f"{item['venue']} 订单 {item['order_id']}：成交额 "
            f"{item['notional_usdc']:.8f} × {item['fee_bps']:g} bps ÷ 10000 = "
            f"{item['fees_usdc']:.8f} USDC（费率核验于 {item['checked_at']}）" for item in estimates)
        row.update(provisional_pnl_usdc=float(net), provisional_fees_usdc=float(fees),
            fee_estimates=estimates, provisional_accounting_note=(
                f'暂计净盈亏：价差盈亏 {gross:.8f} − 已核验手续费 {known:.8f} '
                f'− 暂计手续费 {estimated:.8f} + 已核验资金费 {funding:.8f} = {net:.8f} USDC。'
                + detail + '。按最近核验费率估算，不代表历史账单及其他费用已确认；实际账单返回后自动更新。'))
