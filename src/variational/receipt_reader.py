"""Bounded, receive-only network observer for a user-launched Selenium session.

JSON paths below come from a locally verified login schema, never guesses.
No cookies, headers, wallet addresses, signatures, or raw responses are saved.
This does not call private endpoints: it observes responses the webpage made.
"""
import base64
import json
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
from src.execution.orders import decimal, direction
from src.common.market_data import age_seconds
from src.common.market_data import utc_now
from src.variational.omni_contract import OmniContract, ENDPOINTS, SUBMITS, ORDERS, POSITIONS, BALANCE


def field(payload, path):
    if not path or not isinstance(path, str):
        raise ValueError('登录响应字段路径尚未验收')
    value = payload
    for key in path.split('.'):
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


class OmniResponseReader:
    def __init__(self, driver, schema, expected_account_id):
        self.driver, self.schema, self.account_id = driver, schema, expected_account_id
        self.requests, self.quotes, self.receipts = {}, {}, []
        self.account = None
        self.expected = None
        self.contract = OmniContract(expected_account_id, schema.get('fee_policy')) if schema.get('adapter') == 'omni_v1' else None

    def refresh_account_snapshots(self):
        """Only GET account resources in the already logged-in official origin.

        No header/cookie extraction, transaction endpoints, or quote acceptance.
        Responses stay in memory and still pass all identity/completeness checks.
        """
        if not self.contract:
            raise ValueError('只读刷新仅支持已识别的 Omni 合约')
        url = urlsplit(self.driver.current_url)
        if url.scheme != 'https' or url.netloc != 'omni.variational.io':
            raise ValueError('账户查询必须在官网登录会话内')
        for path in (ORDERS, POSITIONS, BALANCE):
            self.contract.snapshots.pop(path, None)
        responses = self.driver.execute_async_script("""
            const done=arguments[arguments.length-1];
            if(location.origin!=='https://omni.variational.io'){done([]);return;}
            const paths=['/api/orders/v2','/api/positions','/api/settlement_pools/details'];
            Promise.all(paths.map(async path=>{
                const control=new AbortController(), timer=setTimeout(()=>control.abort(),4000);
                try{
                    const response=await fetch(path,{method:'GET',credentials:'same-origin',
                        cache:'no-store',redirect:'error',signal:control.signal});
                    if(!response.ok)return {path,status:response.status};
                    const body=await response.text();
                    if(body.length>1000000)return {path,status:413};
                    return {path,status:response.status,payload:JSON.parse(body),observed_at:new Date().toISOString()};
                }catch(e){return {path,status:0};}finally{clearTimeout(timer);}
            })).then(done,()=>done([]));
        """)
        details = {}
        for row in responses or []:
            path = row.get('path')
            if path not in (ORDERS, POSITIONS, BALANCE):
                continue
            payload = row.get('payload')
            details[path] = {'http_status': row.get('status'), 'received': payload is not None}
            if path == ORDERS and isinstance(payload, dict):
                pagination = payload.get('pagination') or {}
                details[path].update(row_count=len(payload['result']) if isinstance(payload.get('result'),list) else None,
                    pagination_fields=sorted(pagination), next_page_empty=pagination.get('next_page') is None,
                    object_count=pagination.get('object_count'),
                    first_offset=(pagination.get('last_page') or {}).get('offset'))
            if payload is not None and 200 <= row.get('status', 0) < 300:
                self.ingest(path, {}, payload, row.get('observed_at'))
            else:
                self.contract.snapshots.pop(path, None)
        return details

    def pump(self):
        allowed = {self.schema.get(key) for key in ('quote_endpoint', 'receipt_endpoint', 'account_endpoint')}
        allowed.discard(None)
        if self.contract:
            allowed = ENDPOINTS
        for event in self.driver.get_log('performance'):
            message = json.loads(event['message'])['message']
            params, method = message.get('params', {}), message.get('method')
            request_id = params.get('requestId')
            if method == 'Network.requestWillBeSent':
                request = params['request']
                url = urlsplit(request['url'])
                if url.scheme == 'https' and url.netloc == 'omni.variational.io' and url.path in allowed:
                    self.requests[request_id] = {'path': url.path, 'wall': params['wallTime'],
                                                'mono': params['timestamp'], 'body': request.get('postData')}
                    if len(self.requests) > 100:
                        self.requests.pop(next(iter(self.requests)))
            elif method == 'Network.responseReceived' and request_id in self.requests:
                request = self.requests[request_id]
                response = params['response']
                request['status'] = response['status']
                request['at'] = request['wall'] + params['timestamp'] - request['mono']
            elif method == 'Network.loadingFinished' and request_id in self.requests:
                request = self.requests.pop(request_id)
                if self.contract and request['path'] in SUBMITS and self.contract.armed_at:
                    started_at = datetime.fromtimestamp(request['wall'], timezone.utc).isoformat()
                    if started_at < self.contract.armed_at:
                        continue
                if not 200 <= request.get('status', 0) < 300 or params.get('encodedDataLength', 0) > 1_000_000:
                    continue
                try:
                    raw = self.driver.execute_cdp_cmd('Network.getResponseBody', {'requestId': request_id})
                    body = base64.b64decode(raw['body']).decode() if raw.get('base64Encoded') else raw['body']
                    if len(body) > 1_000_000:
                        continue
                    at = datetime.fromtimestamp(request['at'], timezone.utc).isoformat()
                    self.ingest(request['path'], json.loads(request['body'] or '{}'), json.loads(body), at)
                except (KeyError, ValueError, TypeError):
                    continue  # Unknown login schemas never become executable observations.

    def ingest(self, endpoint, request, payload, observed_at):
        if self.contract:
            self.contract.ingest(endpoint, request, payload, observed_at)
            self.quotes = self.contract.quotes
            return
        age = age_seconds(observed_at)
        if age is None or not 0 <= age <= 10:
            return
        if endpoint == self.schema.get('quote_endpoint'):
            instrument = request.get('instrument', {})
            symbol, quantity = instrument.get('underlying'), request.get('qty')
            if symbol not in ('BTC', 'ETH') or instrument.get('instrument_type') != 'perpetual_future':
                return
            mapping = self.schema['quote_fields']
            quote = {key: field(payload, path) for key, path in mapping.items()}
            if quote.get('account_id') != self.account_id or not quote.get('quote_id'):
                return
            if quote.get('symbol') != symbol or decimal(quote['size']) != decimal(quantity):
                return
            quote.update(instrument_type='perpetual', updated_at=observed_at,
                         executable=self.schema.get('executable_quote_verified') is True)
            self.quotes[symbol] = quote
        elif endpoint == self.schema.get('account_endpoint'):
            account = {key: field(payload, path) for key, path in self.schema['account_fields'].items()}
            if account.get('account_id') == self.account_id:
                account['observed_at'] = observed_at
                self.account = account
        elif endpoint == self.schema.get('receipt_endpoint') and self.expected:
            # A successful HTTP response alone is never a successful order.
            values = field(payload, self.schema['receipt_collection'])
            for raw in values:
                receipt = {key: field(raw, path) for key, path in self.schema['receipt_fields'].items()}
                if receipt.get('account_id') == self.account_id and receipt.get('quote_id') == self.expected['quote_id']:
                    status = self.schema['terminal_statuses'].get(receipt.get('status'))
                    if status:
                        receipt.update(status=status, confirmed=True)
                        self.receipts.append(receipt)
                        self.receipts = self.receipts[-20:]

    def executable_quote(self, symbol, quantity):
        self.pump()
        quote = self.quotes.get(symbol)
        if self.contract and quote:
            try:
                self.account = self.contract.account()
            except ValueError:
                self.refresh_account_snapshots()
                self.account = self.contract.account()
            quote = dict(quote, executable=self.schema.get('executable_quote_verified') is True
                         and self.schema.get('account_session_verified') is True and quote.get('fee_bps') == 0)
        if not quote or decimal(quote['size']) != decimal(quantity) or not quote.get('executable'):
            raise ValueError('未收到该账户、该数量的可执行 RFQ')
        return dict(quote)

    def mark_price(self, symbol):
        self.pump()
        quote = self.quotes.get(symbol)
        if not quote:
            raise ValueError('无对应市场参考价')
        return float(decimal(quote['mark_price'], True))

    def validate_account(self, account_id, symbol, quantity, reduce_only=False, side=None):
        self.pump()
        if self.contract:
            self.refresh_account_snapshots()
            self.account = self.contract.account()
        account = self.account
        age = age_seconds(account.get('observed_at')) if account else None
        if not account or account_id != account['account_id'] or age is None or not 0 <= age <= 5:
            raise ValueError('账户快照未确认或过期')
        if account['pending_orders'] != 0:
            raise ValueError('账户存在活动订单')
        positions = account['positions']
        if not isinstance(positions, dict):
            raise ValueError('持仓字典格式未验收')
        current = decimal(positions.get(symbol, 0))
        if reduce_only:
            if abs(current) < decimal(quantity) or current == 0 or direction(side) != ('sell' if current > 0 else 'buy'):
                raise ValueError('只减仓方向或数量不符')
        elif current != 0 or decimal(account['available_balance']) < decimal(quantity) * decimal(self.mark_price(symbol)):
            raise ValueError('已有持仓或可用余额不足')

    def arm(self, quote, side, size, reduce_only):
        if self.contract:
            self.contract.arm(quote, side, size, reduce_only, utc_now())
        self.expected = dict(quote_id=quote['quote_id'], side=direction(side), size=size, reduce_only=reduce_only)
        self.receipts.clear()

    def wait_terminal(self, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.pump()
            if self.contract:
                if self.contract.error:
                    raise ValueError(self.contract.error)
                if self.contract.receipt:
                    return dict(self.contract.receipt)
            if self.receipts:
                return self.receipts[-1]
            time.sleep(.1)
        raise TimeoutError('网页未返回可核验的成交终态')
