#!/usr/bin/env python3
"""Attach to an existing local Chrome session and inspect indicative quotes.

This script is read-only: it does not navigate, fill forms, click buttons,
save cookies, or persist response bodies. It reports only safe quote freshness
signals so a user can distinguish repeated cached data from a changing quote.
"""
import argparse
import base64
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.execution.browser_session import select_market_tab, valid_debugger_address
from src.variational.schema_audit import safe_path


def parse_timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        normalized = value.replace('Z', '+00:00')
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return None
        return (datetime.now(timezone.utc) - parsed).total_seconds()
    except (TypeError, ValueError):
        return None


def quote_view(body, request_body):
    if not isinstance(body, dict):
        return None
    request = {}
    if isinstance(request_body, str):
        try:
            request = json.loads(request_body)
        except (TypeError, ValueError):
            request = {}
    instrument = body.get('instrument') if isinstance(body.get('instrument'), dict) else {}
    if not instrument and isinstance(request.get('instrument'), dict):
        instrument = request['instrument']
    quote = body
    for envelope in ('result', 'quote'):
        nested = body.get(envelope)
        if isinstance(nested, dict):
            quote = nested
            break
    symbol = instrument.get('underlying')
    instrument_type = instrument.get('instrument_type')
    if symbol not in ('BTC', 'ETH') or instrument_type != 'perpetual_future':
        return None
    timestamp = quote.get('timestamp')
    safe_payload = {
        'symbol': symbol,
        'instrument_type': instrument_type,
        'qty': request.get('qty'),
        'bid': quote.get('bid'),
        'ask': quote.get('ask'),
        'timestamp': timestamp,
        'quote_id_present': bool(quote.get('quote_id')),
    }
    digest = hashlib.sha256(
        json.dumps(safe_payload, sort_keys=True, separators=(',', ':')).encode()
    ).hexdigest()[:12]
    return {
        'symbol': symbol,
        'qty': request.get('qty'),
        'bid_present': quote.get('bid') is not None,
        'ask_present': quote.get('ask') is not None,
        'quote_id_present': bool(quote.get('quote_id')),
        'server_timestamp_age_s': parse_timestamp(timestamp),
        'body_digest': digest,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--address', default='127.0.0.1:9222',
                        help='已有 Chrome 的本机 CDP 地址，不会启动新浏览器')
    parser.add_argument('--symbol', choices=('BTC', 'ETH'), default='ETH')
    parser.add_argument('--seconds', type=int, default=120)
    args = parser.parse_args()
    if not valid_debugger_address(args.address) or not 10 <= args.seconds <= 600:
        raise SystemExit('只允许 127.0.0.1:有效端口；采集时长 10–600 秒')

    from selenium import webdriver

    options = webdriver.ChromeOptions()
    options.debugger_address = args.address
    options.set_capability('goog:loggingPrefs', {'performance': 'ALL'})
    driver = None
    try:
        driver = webdriver.Chrome(options=options)
        select_market_tab(driver, args.symbol)
        driver.execute_cdp_cmd('Network.enable', {})
        print('已连接现有 Chrome；只读监控开始。请在网页手动刷新并输入数量，不要提交订单。', flush=True)
        print('字段含义：server_timestamp_age 是平台返回时间戳年龄；body_digest 不变表示安全字段未变化。', flush=True)
        pending = {}
        previous_digest = None
        until = time.monotonic() + args.seconds
        while time.monotonic() < until:
            for item in driver.get_log('performance'):
                event = json.loads(item['message'])['message']
                params = event.get('params', {})
                method = event.get('method')
                request_id = params.get('requestId')
                if method == 'Network.requestWillBeSent' and request_id:
                    request = params.get('request', {})
                    url = request.get('url', '')
                    path = safe_path(url)
                    if path and '/quotes/indicative' in path:
                        pending[request_id] = {
                            'path': path,
                            'request_body': request.get('postData'),
                        }
                elif method == 'Network.responseReceived' and request_id in pending:
                    response = params.get('response', {})
                    pending[request_id].update({
                        'status': response.get('status'),
                        'from_disk_cache': bool(response.get('fromDiskCache')),
                        'from_service_worker': bool(response.get('fromServiceWorker')),
                        'headers': {str(key).lower(): value for key, value in
                                    (response.get('headers') or {}).items()
                                    if str(key).lower() in {'age', 'cache-control', 'etag', 'date'}},
                    })
                elif method == 'Network.loadingFinished' and request_id in pending:
                    metadata = pending.pop(request_id)
                    if metadata.get('status') not in range(200, 300):
                        continue
                    try:
                        raw = driver.execute_cdp_cmd(
                            'Network.getResponseBody', {'requestId': request_id})
                        body_text = (base64.b64decode(raw['body']).decode()
                                     if raw.get('base64Encoded') else raw['body'])
                        view = quote_view(json.loads(body_text), metadata.get('request_body'))
                    except (KeyError, TypeError, ValueError, UnicodeError):
                        continue
                    if view is None:
                        continue
                    changed = previous_digest != view['body_digest']
                    previous_digest = view['body_digest']
                    print(json.dumps({
                        'observed_at': datetime.now(timezone.utc).isoformat(),
                        'endpoint': metadata['path'],
                        'status': metadata.get('status'),
                        'qty': view['qty'],
                        'bid_present': view['bid_present'],
                        'ask_present': view['ask_present'],
                        'quote_id_present': view['quote_id_present'],
                        'server_timestamp_age_s': view['server_timestamp_age_s'],
                        'body_changed': changed,
                        'from_disk_cache': metadata.get('from_disk_cache', False),
                        'from_service_worker': metadata.get('from_service_worker', False),
                        'cache_headers': metadata.get('headers', {}),
                    }, ensure_ascii=False), flush=True)
            time.sleep(0.25)
    finally:
        if driver:
            driver.service.stop()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('只读监控已停止；浏览器保持打开。')
    except Exception as exc:
        raise SystemExit('未能连接已有 Chrome：' + type(exc).__name__ +
                         '；确认 Chrome 已以 127.0.0.1 调试端口启动。') from None
