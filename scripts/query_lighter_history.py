#!/usr/bin/env python3
"""Explicit, read-only Lighter order-history query for the dashboard."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values
from src.common.config_loader import ConfigLoader
from src.execution.platform_clients import LighterCredentials, build_lighter_session
from src.execution.journal import OrderJournal
from src.lighter.execution import LighterExecutionAdapter


def safe_order(row):
    """Keep only public order/fill fields; never print signed credentials."""
    if not isinstance(row, dict):
        return {}
    keys = (
        'order_id', 'order_index', 'client_order_index', 'market_id',
        'status', 'is_ask', 'reduce_only', 'initial_base_amount',
        'filled_base_amount', 'filled_quote_amount', 'price', 'average_price',
        'created_at', 'timestamp', 'updated_at', 'remaining_base_amount',
    )
    result = {key: row.get(key) for key in keys if key in row}
    if 'average_price' not in result:
        try:
            filled = float(result.get('filled_base_amount') or 0)
            quote = float(result.get('filled_quote_amount') or 0)
            if filled > 0 and quote > 0:
                result['average_price'] = quote / filled
        except (TypeError, ValueError):
            pass
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--venues', required=True, help='lighter,lighter_rh')
    parser.add_argument('--symbols', required=True, help='comma separated symbols')
    parser.add_argument('--limit', type=int, default=100)
    args = parser.parse_args()
    venues = list(dict.fromkeys(item.strip() for item in args.venues.split(',') if item.strip()))
    symbols = list(dict.fromkeys(item.strip().upper().replace('-PERP', '')
                                for item in args.symbols.split(',') if item.strip()))
    if len(venues) != 2 or any(item not in ('lighter', 'lighter_rh') for item in venues):
        raise SystemExit('只支持 lighter,lighter_rh 两个平台')
    if not symbols or len(symbols) > 20:
        raise SystemExit('品种数量需为 1 到 20')
    limit = max(1, min(int(args.limit), 100))
    config = ConfigLoader(ROOT / 'config/config.yaml')
    values = dotenv_values(ROOT / '.env')
    journal = OrderJournal(ROOT / 'data/execution_orders.sqlite')
    clients, adapters = [], []
    rows, errors = [], []
    try:
        for venue in venues:
            prefix = 'LIGHTER_RH_' if venue == 'lighter_rh' else 'LIGHTER_'
            account = values.get(prefix + 'ACCOUNT_INDEX')
            key_index = values.get(prefix + 'API_KEY_INDEX')
            private_key = values.get(prefix + 'API_PRIVATE_KEY')
            if account is None or key_index is None or not private_key:
                errors.append({'venue': venue, 'error': '凭据未配置'})
                continue
            credentials = LighterCredentials(int(account), int(key_index), private_key)
            public, sdk = build_lighter_session(venue, credentials,
                                                 config.get('common.network.proxy_url'))
            clients.extend([public, sdk])
            adapter = LighterExecutionAdapter(public, sdk, journal, int(account),
                                               enabled=False, venue=venue)
            adapters.append((venue, adapter))
            for symbol in symbols:
                try:
                    market, _ = public.get_market(symbol)
                    orders = adapter.history_orders(symbol, limit=limit, max_pages=10)
                    for order in orders:
                        rows.append({'venue': venue, 'symbol': symbol,
                                     'market_id': market.get('market_id'),
                                     **safe_order(order)})
                except Exception as exc:
                    errors.append({'venue': venue, 'symbol': symbol,
                                   'error': type(exc).__name__})
    finally:
        for client in reversed(clients):
            try:
                client.close()
            except Exception:
                pass
    rows.sort(key=lambda item: str(item.get('timestamp') or item.get('updated_at') or
                                   item.get('created_at') or ''), reverse=True)
    print(json.dumps({'success': not errors, 'orders_submitted': False,
                      'venues': venues, 'symbols': symbols, 'rows': rows,
                      'errors': errors}, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    main()
