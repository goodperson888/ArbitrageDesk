#!/usr/bin/env python3
"""Fetch fill evidence for history display. This script never submits orders."""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import dotenv_values
from src.common.config_loader import ConfigLoader
from src.execution.history_recovery import normalized_order, recover_leg, timestamp
from src.execution.history_accounting import (arcus_pages, enrich_arcus_orders, enrich_lighter_order,
                                               journal_fees, epoch_ms, funding_record, store_evidence)
from src.execution.history import TradeHistory
from src.web.execution_book import pair_sources
from src.execution.platform_clients import LighterCredentials, build_lighter_session
from src.arcus.execution import ArcusClient
from src.execution.history_receipts import historical_market_id


def sdk_read(public, sdk, *args):
    """SDK audit reads must respect the same host cooldown as public REST."""
    public.client.rate_limit.check()
    try:
        return sdk.call(*args)
    except Exception as exc:
        if getattr(exc, 'status', None) == 429:
            public.client.rate_limit.reject(SimpleNamespace(status_code=429, headers=getattr(exc, 'headers', None) or {}))
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--venues', required=True)
    parser.add_argument('--symbols', required=True)
    parser.add_argument('--limit', type=int, default=100)
    args = parser.parse_args()
    venues = list(dict.fromkeys(args.venues.split(',')))
    symbols = list(dict.fromkeys(args.symbols.upper().split(',')))
    if len(venues) != 2 or any(v not in ('lighter', 'lighter_rh', 'arcus') for v in venues) or not 1 <= len(symbols) <= 20:
        raise ValueError('历史查询平台或品种无效')
    values = dotenv_values(ROOT / '.env')
    config = ConfigLoader(ROOT / 'config/config.yaml')
    proxy = config.get('common.network.proxy_url')
    positions = []
    for path, pair in pair_sources(ROOT / 'data', venues):
        for position in json.loads(path.read_text()).get('positions', []):
            if position.get('symbol') in symbols:
                positions.append({**position, 'variational_venue': position.get('variational_venue') or pair[0],
                                  'lighter_venue': position.get('lighter_venue') or pair[1]})
    receipts = TradeHistory(ROOT / 'data/hedge_events.sqlite').receipt_orders(p['id'] for p in positions)
    for position in positions:
        evidence = receipts.get(position['id'], {})
        position['entry_order_ids'] = evidence.get('entry', [])
        position['entry_order_ids_by_venue'] = evidence.get('entry_by_venue', {})
        position['close_order_ids_by_venue'] = evidence.get('close_by_venue', {})
    rows, complete, errors, funding = [], [], [], []
    for venue in venues:
        clients = []
        try:
            if venue == 'arcus':
                client = ArcusClient(values['ARCUS_WALLET_ADDRESS'], int(values['ARCUS_ACCOUNT_INDEX']),
                                     values['ARCUS_API_SIGNING_KEY'],
                                     base_url=config.get('arcus.api.base_url', 'https://api.arcus.xyz'), proxy_url=proxy)
                clients.append(client)
                payload = client.orders()
                raw = payload.get('orders')
                if not isinstance(raw, list) or 'total' not in payload or int(payload['total']) != len(raw):
                    raise ValueError('Arcus 历史未完整返回')
                for symbol in symbols:
                    for row in raw:
                        if row.get('marketDisplayName') == symbol + '-USD':
                            item = normalized_order(venue, symbol, row)
                            if item:
                                rows.append(item)
                    complete.append([venue, symbol])
                arcus_orders = [r for r in rows if r['venue'] == venue]
                if arcus_orders:
                    start = min(epoch_ms(r['timestamp']) for r in arcus_orders if r.get('timestamp')) * 1000 - 86400000000
                    end = int(datetime.now(timezone.utc).timestamp() * 1000000)
                    try:
                        fills = arcus_pages(client, '/v1/fills', 'fills', start, end)
                        enrich_arcus_orders(arcus_orders, fills)
                    except Exception as exc:
                        errors.append({'venue': venue, 'stage': 'fills', 'error': type(exc).__name__})
                    try:
                        payments = arcus_pages(client, '/v1/funding', 'fundingPayments', start, end)
                        for symbol in symbols:
                            funding.append(funding_record(venue, symbol, timestamp(start), timestamp(end),
                                [r for r in payments if r.get('marketDisplayName') == symbol + '-USD'], 'payment', 'time'))
                    except Exception as exc:
                        errors.append({'venue': venue, 'stage': 'funding', 'error': type(exc).__name__})
            else:
                prefix = 'LIGHTER_RH_' if venue == 'lighter_rh' else 'LIGHTER_'
                public, sdk = build_lighter_session(venue, LighterCredentials(
                    int(values[prefix + 'ACCOUNT_INDEX']), int(values[prefix + 'API_KEY_INDEX']),
                    values[prefix + 'API_PRIVATE_KEY']), proxy)
                clients.extend([public, sdk])
                for symbol in symbols:
                    try:
                        market_id = historical_market_id(ROOT / 'data/execution_orders.sqlite', venue,
                                                        int(values[prefix + 'ACCOUNT_INDEX']), symbol)
                        if market_id is None:
                            market, _ = public.get_market(symbol)
                            market_id = market['market_id']
                        raw = sdk_read(public, sdk, 'inactive_orders', market_id, max(1, min(args.limit, 100)), 10)
                        items = [normalized_order(venue, symbol, row) for row in raw]
                        items = [item for item in items if item]
                        rows.extend(items)
                        complete.append([venue, symbol])
                        matched_ids = set()
                        relevant = [p for p in positions if p['symbol'] == symbol]
                        for position in relevant:
                            for leg in ('variational', 'lighter'):
                                if position.get(leg + '_venue') != venue:
                                    continue
                                fill = recover_leg(position, leg, items)
                                if fill:
                                    matched_ids.update(fill['entry_order_ids'] + fill['order_ids'])
                        for item in items:
                            if item['order_id'] in matched_ids:
                                try:
                                    trades = sdk_read(public, sdk, 'trades', market_id, int(item['order_id']))
                                    enrich_lighter_order(item, trades, int(values[prefix + 'ACCOUNT_INDEX']), market_id)
                                except Exception as exc:
                                    errors.append({'venue': venue, 'symbol': symbol, 'stage': 'fills', 'error': type(exc).__name__})
                        for position in relevant:
                            for leg in ('variational', 'lighter'):
                                if position.get(leg + '_venue') != venue:
                                    continue
                                fill = recover_leg(position, leg, items)
                                if not fill:
                                    continue
                                start, end = fill['entry_timestamp'], fill['timestamp']
                                try:
                                    payments = sdk_read(public, sdk, 'position_funding', market_id, epoch_ms(start), epoch_ms(end) + 1)
                                    # API millisecond bounds are rounded outwards; keep
                                    # only payments inside the exact fill interval.
                                    payments = [r for r in payments if start <= (timestamp(r.get('timestamp')) or '') <= end]
                                    funding.append(funding_record(venue, symbol, start, end, payments, 'change', 'timestamp'))
                                except Exception as exc:
                                    errors.append({'venue': venue, 'symbol': symbol, 'stage': 'funding', 'error': type(exc).__name__})
                    except Exception as exc:
                        errors.append({'venue': venue, 'symbol': symbol, 'error': type(exc).__name__})
        except Exception as exc:
            errors.append({'venue': venue, 'error': type(exc).__name__})
        finally:
            for client in reversed(clients):
                try:
                    client.close()
                except Exception:
                    pass
    journal_fees(ROOT / 'data/execution_orders.sqlite', rows)
    store_evidence(ROOT / 'data', rows, complete, funding, errors)
    print(json.dumps({'success': not errors, 'orders_submitted': False, 'venues': venues, 'symbols': symbols,
                      'rows': rows, 'errors': errors, 'funding_intervals': len(funding),
                      'message': '已读取平台成交、手续费和持仓期间资金费；完整证据自动核算净盈亏，缺失项逐笔列明。' +
                                 ('部分市场读取失败，已保留此前证据。' if errors else '')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
