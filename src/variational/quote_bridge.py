"""In-memory, allowlisted indicative quotes from the user's browser extension."""
from src.common.platform_support import open_private_read, private_fchmod
import secrets
import threading
import os
import re
from pathlib import Path
from src.common.market_data import number, age_seconds, utc_now


class QuoteBridge:
    def __init__(self, token_path=None):
        self.token = self._load_token(token_path)
        self.lock = threading.RLock()
        self.quotes = {}
        self.last_received_at = None
        self.last_rejection = None

    @staticmethod
    def _load_token(token_path):
        if token_path is None:
            return secrets.token_urlsafe(32)
        path = Path(token_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            fd = open_private_read(path)
            with os.fdopen(fd) as stream:
                private_fchmod(stream.fileno())
                token = stream.read(101)
            if not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
                raise ValueError('本机桥接配对文件损坏；请重新生成配对文件')
            return token
        token = secrets.token_urlsafe(32)
        with os.fdopen(fd, 'w') as stream:
            stream.write(token)
            stream.flush()
            os.fsync(stream.fileno())
        return token

    def _reject(self, reason, symbol=None, timestamp=None):
        with self.lock:
            self.last_rejection = {
                'reason': reason,
                'symbol': symbol,
                'quote_timestamp': timestamp,
                'rejected_at': utc_now(),
            }
        raise ValueError(reason)

    def ingest(self, payload):
        if not isinstance(payload, dict) or payload.get('source_url') != 'https://omni.variational.io/api/quotes/indicative':
            raise ValueError('仅接受已识别的 Variational indicative 报价')
        symbol = payload.get('symbol')
        if symbol not in ('BTC', 'ETH'):
            raise ValueError('目前仅验证 BTC / ETH 永续报价')
        if payload.get('instrument_type') != 'perpetual_future':
            raise ValueError('合约类型不匹配')
        qty = number(payload.get('qty'), True)
        bid, ask = number(payload.get('bid'), True), number(payload.get('ask'), True)
        if bid > ask:
            raise ValueError('买卖价倒置')
        # observed_at is browser observation, not proof of exchange freshness.
        observed = payload.get('observed_at')
        age = age_seconds(observed)
        if age is None or not 0 <= age <= 10:
            raise ValueError('网页报价时间无效或已过期')
        timestamp = payload.get('timestamp')
        server_age = age_seconds(timestamp)
        if timestamp is not None and server_age is None:
            self._reject('服务端报价时间格式无效或来自未来', symbol, timestamp)
        if server_age is not None and not 0 <= server_age <= 10:
            self._reject(f'服务端报价已过期（约 {server_age:.1f} 秒）', symbol, timestamp)
        page = None
        metadata = {}
        if 'estimated_fees_bid' in payload or 'estimated_fees_ask' in payload:
            fees = {side: number(payload.get('estimated_fees_'+side)) for side in ('bid', 'ask')}
            if any(value < 0 for value in fees.values()):
                raise ValueError('预计费用不能为负')
            metadata['estimated_fees_raw'] = fees
            # Zero is dimension independent; nonzero units remain unverified.
            metadata['fee_bps'] = 0 if all(value == 0 for value in fees.values()) else None
            metadata['fee_source'] = '官网当前数量报价预计费用；非成交结算'
        if 'qty_limits' in payload:
            limits = {}
            for side in ('bid', 'ask'):
                raw = payload['qty_limits'].get(side, {})
                item = {key: number(raw.get(key), True) for key in ('min_qty_tick','min_qty','max_qty')}
                if item['min_qty'] > item['max_qty']:
                    raise ValueError('数量限制倒置')
                limits[side] = item
            metadata['qty_limits'] = limits
        if 'page_funding_bps' in payload:
            rate = number(payload['page_funding_bps'])
            period = number(payload.get('page_funding_interval_s'))
            if abs(rate) > 1000 or period not in (3600,7200,10800,14400,18000,21600,25200,28800):
                raise ValueError('官网资金费周期或费率无效')
            metadata['funding'] = {'rate_bps': rate, 'interval_s': period,
                                   'observed_at': observed, 'source':'官网可见资金费率；非已结算资金费'}
            if payload.get('page_funding_next_in_s') is not None:
                remaining = number(payload['page_funding_next_in_s'])
                if not 0 <= remaining <= period:
                    raise ValueError('资金费倒计时不在本周期内')
                metadata['funding']['next_in_s'] = remaining
        if payload.get('page_side') in ('buy', 'sell'):
            estimate = number(payload.get('page_estimated_slippage_bps'))
            maximum = number(payload.get('page_max_slippage_bps'))
            if not -10000 <= estimate <= 10000 or not 0 <= maximum <= 10000:
                raise ValueError('网页滑点显示值超出范围')
            page = {'side': payload['page_side'], 'estimated_bps': estimate, 'max_bps': maximum,
                    'observed_at': observed, 'source': '官网可见控件（与报价数量/买卖价匹配）'}
        with self.lock:
            previous = self.quotes.get(symbol)
            if previous and observed <= previous['observed_at']:
                raise ValueError('重复或乱序报价')
            self.quotes[symbol] = {'bid': bid, 'ask': ask, 'quote_size': qty,
                'quote_notional_usdc': qty * ask, 'quote_updated_at': timestamp if server_age is not None else observed,
                'quote_timestamp_kind': 'exchange' if server_age is not None else 'browser_observed',
                'quote_source': 'variational_browser_quote', 'observed_at': observed,
                'quote_id_present': bool(payload.get('quote_id_present')), 'executable': False}
            self.quotes[symbol].update(metadata)
            if page is not None:
                self.quotes[symbol]['page_slippage'] = page
            self.last_received_at = utc_now()
            self.last_rejection = None

    def snapshot(self, symbol):
        with self.lock:
            quote = self.quotes.get(symbol)
            age = age_seconds(quote['observed_at']) if quote else None
            if age is not None and 0 <= age <= 10:
                return dict(quote)
            return None

    def status(self):
        quotes = {s:self.snapshot(s) for s in ('BTC','ETH')}
        return {'last_received_at': self.last_received_at,
                'symbols': [s for s,q in quotes.items() if q],
                'metadata': {s:{'fee_known':q.get('fee_bps') is not None,
                                'quantity_limits_known':bool(q.get('qty_limits')),
                                'funding_known':bool(q.get('funding'))} for s,q in quotes.items() if q},
                'last_rejection': dict(self.last_rejection) if self.last_rejection else None,
                'note': '只读接收网页参考报价；不读取钱包、Cookie、签名或执行订单'}
