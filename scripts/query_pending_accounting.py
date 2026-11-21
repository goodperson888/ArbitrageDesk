#!/usr/bin/env python3
"""Read at most two missing accounting items. Never submit or cancel orders."""
import copy
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values
from src.arcus.execution import ArcusClient
from src.common.config_loader import ConfigLoader
from src.common.market_data import utc_now
from src.common.rate_limit import RateLimitError
from src.execution.pending_accounting import pending_jobs, read_allowed, POSITION_FIELDS
from src.execution.platform_clients import LighterCredentials, build_lighter_session
from src.execution.history_receipts import historical_market_id
from src.execution.history_accounting import (enrich_arcus_orders, enrich_lighter_order,
    epoch_ms, funding_record, store_evidence)
from src.execution.history_recovery import timestamp
from src.execution.history_recovery import recover_leg, settle_history


def refresh(root, position):
    root = Path(root)
    if not position.get('id') or set(position) - set(POSITION_FIELDS):
        raise ValueError('核账记录无效')
    cache = json.loads((root / 'data/platform-order-history.json').read_text())
    jobs = pending_jobs(position, cache)
    report = dict(success=True, orders_submitted=False, complete=not jobs, reads=0, errors=[])
    if not jobs:
        settled = dict(position)
        recovered = {leg: recover_leg(position, leg, cache.get('rows', [])) for leg in ('variational', 'lighter')}
        settle_history(settled, {leg: fill for leg, fill in recovered.items() if fill}, cache.get('funding_intervals', []))
        report.update(complete=not settled['accounting_pending'],
                      message='已复用完整账务证据' if not settled['accounting_pending'] else '成交证据不足或费用仍待核对，空闲时继续完整补查')
        return report
    if not read_allowed(root):
        return dict(report, deferred=True, complete=False, message='交易或持仓核验进行中，账务补查已延后')
    check = (cache.get('accounting_checks') or {}).get(position['id'], {})
    offset = int(check.get('cursor', 0)) % len(jobs)
    batch = (jobs[offset:] + jobs[:offset])[:2]
    values = dotenv_values(root / '.env')
    config = ConfigLoader(root / 'config/config.yaml')
    proxy = config.get('common.network.proxy_url')
    clients, sessions = [], {}
    rows, funding, complete = [], [], []
    try:
        for job in batch:
            if not read_allowed(root):
                report['deferred'] = True
                break
            venue, symbol = job['venue'], job['symbol']
            public = sdk = None
            try:
                if venue not in sessions:
                    if venue == 'arcus':
                        client = ArcusClient(values['ARCUS_WALLET_ADDRESS'], int(values['ARCUS_ACCOUNT_INDEX']),
                            values['ARCUS_API_SIGNING_KEY'], base_url=config.get('arcus.api.base_url', 'https://api.arcus.xyz'), proxy_url=proxy)
                        clients.append(client)
                        sessions[venue] = (client, None, None)
                    else:
                        prefix = 'LIGHTER_RH_' if venue == 'lighter_rh' else 'LIGHTER_'
                        account = int(values[prefix + 'ACCOUNT_INDEX'])
                        public, sdk = build_lighter_session(venue, LighterCredentials(account,
                            int(values[prefix + 'API_KEY_INDEX']), values[prefix + 'API_PRIVATE_KEY']), proxy)
                        clients.extend([public, sdk])
                        sessions[venue] = (public, sdk, account)
                public, sdk, account = sessions[venue]
                # Session construction is also outside execution. Yield again
                # if a trade started while the optional session was created.
                if not read_allowed(root):
                    report['deferred'] = True
                    break
                if sdk:
                    public.client.rate_limit.check()
                    if not public.client.rate_limit.optional_read_ready():
                        report['deferred'] = True
                        break
                    market = historical_market_id(root / 'data/execution_orders.sqlite', venue, account, symbol)
                    if market is None:
                        report['deferred'] = True
                        break  # No extra instrument/catalog request during live execution.
                    report['reads'] += 1
                    if job['kind'] == 'fills':
                        trades = sdk.call('trades', market, int(job['order']['order_id']), 1)
                        row = copy.deepcopy(job['order'])
                        enrich_lighter_order(row, trades, account, market)
                        rows.append(row)
                    else:
                        payments = sdk.call('position_funding', market, epoch_ms(job['start']), epoch_ms(job['end']) + 1, 1)
                        payments = [r for r in payments if job['start'] <= (timestamp(r.get('timestamp')) or '') <= job['end']]
                        funding.append(funding_record(venue, symbol, job['start'], job['end'], payments, 'change', 'timestamp'))
                else:
                    public.rate_limit.check()
                    if not public.rate_limit.optional_read_ready():
                        report['deferred'] = True
                        break
                    if job['kind'] == 'fills':
                        row = copy.deepcopy(job['order'])
                        start = row.get('first_fill_at')
                        if not start:
                            report['deferred'] = True
                            continue
                        start_us = int(datetime.fromisoformat(start).timestamp() * 1000000)
                        end_us = int(datetime.fromisoformat(row['timestamp']).timestamp() * 1000000)
                        report['reads'] += 1
                        payload = public._get('/v1/fills', dict(address=public.wallet_address,
                            accountIndex=public.account_index, **{'from': start_us - 1, 'to': end_us + 1, 'limit': 100}))
                        enrich_arcus_orders([row], payload['fills'])
                        rows.append(row)
                    else:
                        start_us = int(datetime.fromisoformat(job['start']).timestamp() * 1000000)
                        end_us = int(datetime.fromisoformat(job['end']).timestamp() * 1000000)
                        report['reads'] += 1
                        payload = public._get('/v1/funding', dict(address=public.wallet_address,
                            accountIndex=public.account_index, **{'from': start_us, 'to': end_us, 'limit': 100}))
                        payments = payload['fundingPayments']
                        if len(payments) >= 100:
                            raise ValueError('资金费需要完整分页，延后补查')
                        funding.append(funding_record(venue, symbol, job['start'], job['end'],
                            [r for r in payments if r.get('marketDisplayName') == symbol + '-USD'], 'payment', 'time'))
                complete.append([venue, symbol])
            except Exception as exc:
                if sdk and getattr(exc, 'status', None) == 429:
                    try:
                        public.client.rate_limit.reject(SimpleNamespace(status_code=429, headers=getattr(exc, 'headers', None) or {}))
                    except RateLimitError:
                        pass
                report['errors'].append(dict(venue=venue, stage=job['kind'], error=type(exc).__name__))
                break
    finally:
        for client in reversed(clients):
            try:
                client.close()
            except Exception:
                pass
    if not report['reads'] and report.get('deferred') and not report['errors']:
        return report  # Yielding to trading must not consume an accounting retry window.
    attempts = int(check.get('attempts', 0)) + 1
    cache = store_evidence(root / 'data', rows, complete, funding, report['errors'],
        {position['id']: dict(checked_at=utc_now(), attempts=attempts,
                             retry_after_seconds=min(1800, 600 * 2 ** min(attempts, 2)),
                             cursor=(offset + max(1, report['reads'])) % len(jobs))})
    remaining = pending_jobs(position, cache)
    report.update(success=not report['errors'], complete=not remaining,
        remaining_items=len(remaining), message='缺失账务已补齐' if not remaining else '平台部分账务尚未返回，保留待核账并自动重试')
    return report


if __name__ == '__main__':
    try:
        payload = sys.stdin.read(32769)
        if len(payload) > 32768:
            raise ValueError('核账请求过大')
        report = refresh(ROOT, json.loads(payload))
    except Exception as exc:
        report = dict(success=False, orders_submitted=False, complete=False,
                      errors=[dict(error=type(exc).__name__)], message='本轮账务补查未完成，稍后自动重试')
    print(json.dumps(report, ensure_ascii=False))
