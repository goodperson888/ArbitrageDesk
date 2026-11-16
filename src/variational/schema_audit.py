"""Redact observed response schemas for local integration diagnostics."""
import re
from urllib.parse import urlsplit

SENSITIVE = re.compile(r'token|secret|private|signature|cookie|auth|session|wallet|address|email|name', re.I)
NUMERIC = {'bid', 'ask', 'qty', 'quantity', 'size', 'price', 'filled_size', 'average_price',
           'requested_size', 'fee', 'fee_bps', 'fees_usdc', 'funding_usdc', 'mark_price',
           'available_balance', 'balance', 'pending_orders'}
ENUMS = {'side', 'symbol', 'underlying', 'instrument_type', 'status', 'reduce_only'}


def safe_path(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.netloc != 'omni.variational.io':
        return None
    if not parsed.path.startswith('/api/') or SENSITIVE.search(parsed.path):
        return None
    # Dynamic URL segments may carry account/order identifiers. Retain routing
    # structure without copying those values into the diagnostic report.
    parts = parsed.path.split('/')
    return '/'.join(':id' if (re.fullmatch(r'\d+', part) or
                     re.fullmatch(r'[A-Fa-f0-9-]{16,}', part) or len(part)>64) else part for part in parts)


def describe(value, prefix='', depth=0, output=None):
    output = [] if output is None else output
    if depth > 8 or len(output) >= 150:
        return output
    if isinstance(value, dict):
        for key, item in list(value.items())[:80]:
            if not isinstance(key, str) or not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]{0,63}', key) or SENSITIVE.search(key):
                continue
            describe(item, f'{prefix}.{key}' if prefix else key, depth+1, output)
    elif isinstance(value, list):
        output.append({'path': prefix, 'type': 'array', 'count': len(value)})
        if value:
            describe(value[0], prefix+'.0', depth+1, output)
    else:
        field = prefix.split('.')[-1]
        row = {'path': prefix, 'type': type(value).__name__}
        if field in NUMERIC and re.fullmatch(r'-?\d+(?:\.\d+)?', str(value)):
            row['sample'] = value
        if field in ENUMS and str(value) in ('BTC','ETH','buy','sell','long','short','perpetual_future',
                                             'filled','partial','canceled','rejected','open','True','False'):
            row['sample'] = value
        output.append(row)
    return output[:150]
