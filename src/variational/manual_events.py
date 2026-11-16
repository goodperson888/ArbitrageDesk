"""Allowlisted, redacted observations from a manually operated Variational page."""
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit


ORIGIN = 'https://omni.variational.io'
EVENT_PATHS = {
    '/api/quotes/indicative',
    '/api/orders/new/market',
    '/api/quotes/accept',
    '/api/orders/v2',
    '/api/trades',
    '/api/positions',
    '/api/transfers',
}


def safe_path(value):
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or parsed.netloc != 'omni.variational.io':
        return None
    return parsed.path if parsed.path in EVENT_PATHS else None


def _number(value):
    if value is None or isinstance(value, bool) or value == '':
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _json(value):
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or len(value) > 200000:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, (dict, list)) else None


def _digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:16]


def _instrument(value):
    if not isinstance(value, dict):
        return None, None
    instrument = value.get('instrument')
    if not isinstance(instrument, dict):
        return None, None
    symbol = instrument.get('underlying')
    kind = instrument.get('instrument_type')
    return symbol if symbol in ('BTC', 'ETH') else None, kind


def _result_rows(value):
    if isinstance(value, dict) and isinstance(value.get('result'), list):
        return value['result']
    if isinstance(value, dict) and isinstance(value.get('rows'), list):
        return value['rows']
    if isinstance(value, list):
        return value
    return []


def _identifiers(source, target):
    for key in ('quote_id', 'rfq_id', 'order_id', 'source_rfq', 'source_quote', 'id', 'company', 'pool_location'):
        value = source.get(key)
        digest = source.get(key + '_digest')
        if isinstance(value, str) and value:
            target[key + '_digest'] = hashlib.sha256(value.encode()).hexdigest()[:16]
        elif isinstance(digest, str) and re.fullmatch('[0-9a-f]{16}', digest):
            target[key + '_digest'] = digest


def normalize_manual_event(payload):
    """Return a bounded safe event or None; never return raw request/response data."""
    if not isinstance(payload, dict):
        return None
    path = safe_path(payload.get('url'))
    method = payload.get('method') if payload.get('method') in ('GET', 'POST') else None
    status = payload.get('status')
    if not path or not method or not isinstance(status, int) or not 0 <= status <= 599:
        return None
    body = _json(payload.get('body'))
    if body is None:
        body = payload.get('body') if isinstance(payload.get('body'), dict) else None
    event = {
        'schema_version': 2,
        'observed_at': datetime.now(timezone.utc).isoformat(),
        'endpoint': path,
        'method': method,
        'http_status': status,
        'body_digest': _digest(body) if body is not None else None,
    }
    capture_id = payload.get('capture_id')
    if isinstance(capture_id, str) and re.fullmatch('[0-9a-f-]{36}', capture_id):
        event['capture_id'] = capture_id
    if payload.get('phase') == 'request' and method == 'GET':
        return None
    if path == '/api/quotes/indicative':
        if payload.get('phase') != 'response' or not isinstance(body, dict):
            return None
        event['kind'] = 'quote_response'
        _identifiers(body, event)
        symbol = body.get('symbol') or (body.get('instrument') or {}).get('underlying')
        if symbol in ('BTC', 'ETH'):
            event['symbol'] = symbol
        for key in ('qty', 'bid', 'ask'):
            event[key] = _number(body.get(key))
        return event

    if path in ('/api/orders/new/market', '/api/quotes/accept') and method == 'POST':
        if not isinstance(body, dict):
            return event
        event.update({
            'kind': 'submit_response' if payload.get('phase') == 'response' else 'submit_request',
            'quote_id_present': bool(body.get('quote_id') or body.get('quote_id_present')),
            'side': body.get('side') if body.get('side') in ('buy', 'sell') else None,
            'max_slippage': _number(body.get('max_slippage')),
            'is_reduce_only': body.get('is_reduce_only') if isinstance(body.get('is_reduce_only'), bool) else None,
        })
        _identifiers(body, event)
        if payload.get('phase') == 'response':
            event['rfq_id_present'] = bool(body.get('rfq_id') or body.get('rfq_id_present'))
        return event

    if path == '/api/transfers' and payload.get('phase') != 'response':
        return None
    rows = _result_rows(body)
    event['kind'] = 'snapshot_response'
    count = body.get('row_count') if isinstance(body, dict) else None
    event['row_count'] = count if type(count) is int and count >= len(rows) else len(rows)
    pagination = body.get('pagination') if isinstance(body, dict) else None
    complete = isinstance(body, list) or (isinstance(pagination, dict) and
        'next_page' in pagination and pagination['next_page'] is None and
        (pagination.get('last_page') or {}).get('offset', 0) == 0)
    if isinstance(body, dict) and 'complete' in body:
        complete = body['complete'] is True
    event['complete'] = complete and event['row_count'] <= 20
    safe_rows = []
    for row in rows[:20]:
        if not isinstance(row, dict):
            continue
        row = {**row, **row.get('position_info', {})} if isinstance(row.get('position_info'), dict) else row
        instrument = row.get('instrument') if isinstance(row.get('instrument'), dict) else {}
        symbol = row.get('symbol') or instrument.get('underlying')
        transfer = path == '/api/transfers'
        if transfer:
            safe = {}
            for key in ('asset', 'token', 'fee_asset', 'fee_currency', 'funding_asset', 'funding_currency',
                        'transfer_type', 'transaction_type', 'type', 'status', 'clearing_status'):
                value = row.get(key)
                if isinstance(value, str) and len(value) <= 32 and re.fullmatch(r'[A-Za-z0-9_. -]+', value):
                    safe[key] = value
        else:
            if symbol not in ('BTC', 'ETH'):
                continue
            safe = {'symbol': symbol}
        for key in ('status', 'clearing_status', 'order_type', 'tif', 'side', 'role', 'trade_type'):
            if not transfer and isinstance(row.get(key), str) and len(row[key]) <= 80:
                safe[key] = row[key]
        numeric_keys = ('amount', 'qty', 'fee', 'fee_amount', 'funding_fee', 'funding_payment',
                        'funding_amount', 'settled_funding') if transfer else (
                            'qty', 'price', 'mark_price', 'value', 'avg_entry_price', 'upnl', 'rpnl', 'cum_funding')
        for key in numeric_keys:
            value = _number(row.get(key))
            if value is not None:
                safe[key] = value
        _identifiers(row, safe)
        if safe:
            safe_rows.append(safe)
    event['rows'] = safe_rows
    # A filtered or malformed row cannot establish that the account is flat.
    event['complete'] = event['complete'] and len(safe_rows) == event['row_count']
    if path == '/api/positions' and any('qty' not in row for row in safe_rows):
        event['complete'] = False
    if path == '/api/transfers':
        event['kind'] = 'settlement_response'
    return event
