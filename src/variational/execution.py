"""Exact-selector browser order workflow with quote-bound receipt verification.

No browser is launched on import. The owner supplies a logged-in driver and an
account-verified receipt reader. The monitor's read-only extension has no role
in authorizing an order. No wallet secrets are requested by this module.
"""
import json
import re
import threading
import time
from contextlib import nullcontext
from decimal import Decimal
from src.execution.telemetry import stage, safe_reason
from urllib.parse import urlsplit
from src.execution.orders import decimal, direction, fresh_quote, unknown
from src.common.market_data import utc_now


class OmniForm:
    """Selectors observed on the public BTC page, 2026-09-27.

    Logged-in submit labels, confirmation dialogs and fill rows are deliberately
    not guessed. A modal appearing after submit must be handled by its verified
    profile, otherwise execution returns unknown and cannot be retried.
    """
    requires_page_slippage = True
    def __init__(self, driver):
        self.driver = driver

    def one(self, selector):
        elements = self.driver.find_elements('css selector', selector)
        visible = [e for e in elements if e.is_displayed()]
        if len(visible) != 1:
            raise ValueError('表单控件缺失或不唯一: '+selector)
        return visible[0]

    def field(self, name):
        return self.one('[data-testid="'+name+'"]')

    def input_mode(self, symbol):
        mode = self.field('input-mode-toggle').text.strip()
        if mode not in (symbol, '$'):
            raise ValueError('数量单位不是基础币或美元名义金额，禁止按未知单位下单')
        return mode

    def assert_market(self, symbol):
        url = urlsplit(self.driver.current_url)
        if url.scheme != 'https' or url.netloc != 'omni.variational.io' or url.path != '/perpetual/'+symbol:
            raise ValueError('必须在正确的官方永续交易页面')
        if symbol not in ('BTC', 'ETH'):
            raise ValueError('网页执行仅支持待验收的 BTC/ETH 永续')
        return self.input_mode(symbol)

    def prepare(self, symbol, side, size, reduce_only, price=None):
        mode = self.assert_market(symbol)
        submit = self.field('submit-button')
        if 'wallet' in submit.text.lower():
            raise ValueError('尚未登录或下单不可用')
        button = self.field('ask-price-display' if direction(side) == 'buy' else 'bid-price-display').find_element('xpath', '..')
        if button.get_attribute('aria-pressed') != 'true':
            button.click()
        if button.get_attribute('aria-pressed') != 'true':
            raise ValueError('买卖方向切换未确认')
        self.prepare_quote(symbol, size, price=price, mode=mode)
        check = self.field('reduce-only-checkbox')
        selected = check.find_element('css selector', 'input[type="checkbox"]').is_selected()
        if selected != reduce_only:
            if not check.is_enabled():
                raise ValueError('无法切换只减仓')
            check.click()
        self.verify(symbol, side, size, reduce_only, price=price)

    def prepare_quote(self, symbol, size, price=None, mode=None):
        mode = mode or self.assert_market(symbol)
        quantity = self.field('quantity-input')
        target = decimal(size, True)
        if mode == '$':
            if price is None:
                raise ValueError('美元名义金额模式需要当前方向报价')
            target *= decimal(price, True)
        try:
            unchanged = decimal(quantity.get_attribute('value')) == target
        except Exception:
            unchanged = False
        if not unchanged:
            quantity.clear()
            quantity.send_keys(str(target))

    def configure_execution_form(self, symbol, slippage_bps):
        """Prepare only reversible controls; never invoke the trade submit button."""
        if self.assert_market(symbol) == '$':
            self.field('input-mode-toggle').click()
            end=time.monotonic()+2
            while self.input_mode(symbol)!=symbol and time.monotonic()<end:
                time.sleep(.05)
        if self.assert_market(symbol) != symbol:
            raise ValueError('基础币单位切换未生效')
        target = decimal(slippage_bps, True)
        if target > 100:
            raise ValueError('滑点设置超出已支持范围')
        if self.max_slippage_bps() != target:
            self.field('slippage-button').click()
            field = self.one('input[placeholder="0.0"]')
            field.clear()
            field.send_keys(str(target / 100))
            buttons = self.driver.find_elements('xpath', '//button[normalize-space(.)="确认"]')
            visible = [b for b in buttons if b.is_displayed() and b.is_enabled()]
            if len(visible) != 1:
                raise ValueError('滑点设置确认按钮未识别；未下单')
            visible[0].click()
            end=time.monotonic()+2
            while self.max_slippage_bps()!=target and time.monotonic()<end:
                time.sleep(.05)
        if self.max_slippage_bps() != target:
            raise ValueError('官网滑点上限设置未生效')

    def verify(self, symbol, side, size, reduce_only, price=None):
        mode = self.assert_market(symbol)
        expected = decimal(size, True)
        if mode == '$':
            if price is None:
                raise ValueError('美元名义金额模式需要当前方向报价')
            expected *= decimal(price, True)
        if decimal(self.field('quantity-input').get_attribute('value'), True) != expected:
            raise ValueError('页面数量或名义金额与计划不符')
        if self.field('ask-price-display' if direction(side) == 'buy' else 'bid-price-display').find_element('xpath', '..').get_attribute('aria-pressed') != 'true':
            raise ValueError('页面方向与计划不符')
        if self.field('reduce-only-checkbox').find_element('css selector', 'input[type="checkbox"]').is_selected() != reduce_only:
            raise ValueError('页面只减仓状态与计划不符')

    def max_slippage_bps(self):
        # Verify the site's current limit; never silently change wallet/account settings.
        button = self.field('slippage-button')
        text = button.text or button.get_attribute('textContent') or ''
        match = re.search(
            r'(?:Max(?:imum)?[.]?|最大)\s*[:：]?\s*([0-9]+(?:\.[0-9]*)?|\.[0-9]+)\s*%',
            text,
            re.IGNORECASE,
        )
        if not match:
            raise ValueError('尚未识别当前语言的滑点上限')
        return decimal(match.group(1)) * 100

    def estimated_slippage_bps(self):
        """Read the currently displayed estimate, not the configurable maximum."""
        button = self.field('slippage-button')
        text = button.text or button.get_attribute('textContent') or ''
        match = re.search(
            r'(?:Est(?:imated)?[.]?|估|预计|预估)(?:\s+(?:slippage|滑点))?\s*[:：]?\s*'
            r'([+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+))\s*%',
            text,
            re.IGNORECASE,
        )
        if not match:
            raise ValueError('尚未识别官网当前预计滑点')
        value = decimal(match.group(1)) * 100
        if not -100 <= value <= 100:
            raise ValueError('官网预计滑点超出支持范围')
        return value

    def click_submit(self, expected_label):
        button = self.field('submit-button')
        if button.text.strip() != expected_label or not button.is_enabled():
            raise ValueError('登录态提交按钮文案未匹配已验收配置')
        button.click()


def verify_receipt(receipt, quote, side, quantity, reduce_only):
    """Normalize fills and retain explicit evidence for contractual zero fees."""
    if not isinstance(receipt, dict):
        raise ValueError('缺少成交回执')
    for field, expected in (('symbol', quote['symbol']), ('side', direction(side)),
                            ('quote_id', quote['quote_id']), ('reduce_only', reduce_only)):
        if receipt.get(field) != expected:
            raise ValueError('回执与本次报价/市场/方向不匹配')
    if decimal(receipt['requested_size']) != decimal(quantity) or not receipt.get('order_id'):
        raise ValueError('缺少可核对的订单身份或数量')
    if receipt.get('confirmed') is not True or receipt.get('status') not in ('filled', 'partial', 'canceled', 'rejected'):
        raise ValueError('订单尚无终态')
    filled = decimal(receipt['filled_size'])
    if not 0 <= filled <= decimal(quantity):
        raise ValueError('回执成交数量无效')
    if receipt['status'] == 'filled' and filled != decimal(quantity):
        raise ValueError('完全成交数量不符')
    if receipt['status'] == 'rejected' and filled != 0:
        raise ValueError('拒单却有成交')
    if filled:
        decimal(receipt['average_price'], True)
    if receipt.get('fees_usdc') is not None:
        decimal(receipt['fees_usdc'])
    # Unknown fee/funding must remain unknown for the coordinator to halt/reconcile.
    normalized = {key: receipt.get(key) for key in ('confirmed', 'status', 'order_id')}
    normalized['filled_size'] = float(filled)
    for key in ('average_price', 'fees_usdc', 'funding_usdc'):
        normalized[key] = float(decimal(receipt[key])) if receipt.get(key) is not None else None
    for key in ('fee_basis','fee_policy_url'):
        if receipt.get(key):
            normalized[key] = receipt[key]
    return normalized | {'success': filled > 0}


class VariationalExecutionAdapter:
    live_blocker = '网页执行流程已实现；登录态 RFQ、确认框、账户检查和回执映射需验收'

    def __init__(self, form, reader, journal, account_id, profile, enabled=False, public=None):
        self.form, self.reader, self.journal = form, reader, journal
        self.account_id, self.profile, self.enabled = account_id, profile, enabled
        self.public = public
        self.lock = threading.RLock()
        self.pending = {}

    @property
    def live_ready(self):
        return bool(self.enabled and self.profile.get('account_verified') and
                    self.profile.get('receipt_schema_verified') and self.reader)

    def size_decimals(self, symbol):
        return int(self.profile['size_decimals'][symbol])

    def get_market_price(self, symbol):
        return self.public.get_market_price(symbol) if self.public else self.reader.mark_price(symbol)

    def get_executable_quote(self, symbol, quantity):
        # This observer must await a real qty-bound authenticated RFQ. A public
        # indicative response or a mutable DOM price cannot set executable=True.
        if not self.live_ready:
            raise ValueError('尚无经过验收的账户 RFQ')
        if self.form.assert_market(symbol) != symbol:
            raise ValueError('对冲须使用基础币数量；请将 Variational 大小单位从 $ 切换为 '+symbol)
        # Base-asset mode uses quantity directly; a separate public mark-price
        # request adds latency without contributing to this executable quote.
        self.form.prepare_quote(symbol, quantity, mode=symbol)
        end = time.monotonic() + 5
        while True:
            try:
                quote = self.reader.executable_quote(symbol, quantity)
                fresh_quote(quote, symbol, quantity)
                if not quote.get('quote_id'):
                    raise ValueError('RFQ 编号缺失')
                try:
                    estimate = self.form.estimated_slippage_bps()
                except (AttributeError, TypeError, ValueError):
                    estimate = None
                if isinstance(estimate, (int, float, Decimal)):
                    quote['page_estimated_slippage_bps'] = float(estimate)
                    quote['slippage_source'] = 'variational_page_estimate'
                elif getattr(self.form, 'requires_page_slippage', False) is True:
                    raise ValueError('官网当前预计滑点缺失')
                return quote
            except ValueError:
                if time.monotonic() >= end:
                    raise
                time.sleep(.1)

    def validate_order(self, symbol, quantity):
        if not self.live_ready:
            raise ValueError(self.live_blocker)
        self.reader.validate_account(self.account_id, symbol, quantity)

    def assert_flat(self, symbol):
        self.reader.pump()
        self.reader.refresh_account_snapshots()
        account = self.reader.contract.account() if self.reader.contract else self.reader.account
        from src.common.market_data import age_seconds
        age = age_seconds(account.get('observed_at')) if account else None
        if (not account or account.get('account_id') != self.account_id or age is None or
                not 0 <= age <= 5 or account.get('pending_orders') != 0 or
                not isinstance(account.get('positions'), dict) or
                any(decimal(size) != 0 for size in account['positions'].values())):
            raise ValueError('Variational 当前账户未确认空仓且无挂单')
        return True

    def _stage(self, name, symbol):
        return stage(self.history,name,symbol) if getattr(self,'history',None) is not None else nullcontext()

    def place_order(self, symbol, side, size, price, slippage_bps, client_order_id, quote, reduce_only=False):
        if not self.live_ready:
            return {'success': False, 'confirmed': True, 'status': 'rejected', 'filled_size': 0, 'error': self.live_blocker}
        side = direction(side)
        with self.lock:
            fresh_quote(quote, symbol, size)
            if decimal(price, True) != decimal(quote['ask' if side == 'buy' else 'bid']):
                raise ValueError('价格与本次报价不符')
            if not 0 <= decimal(slippage_bps) <= 100:
                raise ValueError('滑点限制无效')
            intent = {'symbol': symbol, 'side': side, 'size': str(decimal(size, True)),
                      'reduce_only': reduce_only, 'price': str(decimal(price, True)),
                      'slippage_bps': str(decimal(slippage_bps)), 'quote_id': quote['quote_id']}
            row, new = self.journal.reserve('variational', self.account_id, client_order_id, intent)
            if not new:
                return json.loads(row['result']) if row['result'] else unknown('该点击意图已存在，禁止重复点击')
            try:
                with self._stage('V 提交前账户检查',symbol):
                    self.reader.validate_account(self.account_id, symbol, size, reduce_only=reduce_only, side=side)
                with self._stage('V 表单方向与数量准备',symbol):
                    self.form.prepare(symbol, side, size, reduce_only, price=price)
                # Preparing the form may cause a new RFQ: bind to that actual quote.
                current = self.reader.executable_quote(symbol, size)
                fresh_quote(current, symbol, size)
                if current.get('quote_id') != quote['quote_id']:
                    # Variational refreshes RFQs while the form is being
                    # prepared.  Reuse is safe only when the new side price
                    # remains inside the caller's directional protection
                    # bound; otherwise reject before any click.  The new RFQ
                    # is then bound to the submit and receipt checks.
                    old_price = decimal(price, True)
                    new_price = decimal(current['ask' if side == 'buy' else 'bid'], True)
                    limit = decimal(slippage_bps) / 10000
                    allowed = (new_price <= old_price * (1 + limit)
                               if side == 'buy' else
                               new_price >= old_price * (1 - limit))
                    if not allowed:
                        raise ValueError('网页准备后 RFQ 已变化且价格越过保护范围，未提交')
                    quote = current
                    price = float(new_price)
                try:
                    current_estimate = self.form.estimated_slippage_bps()
                except (AttributeError, TypeError, ValueError):
                    current_estimate = None
                if current_estimate is None and getattr(self.form, 'requires_page_slippage', False) is True:
                    raise ValueError('官网当前预计滑点缺失，未提交')
                if current_estimate is not None and type(current_estimate) in (int, float) and current_estimate > decimal(slippage_bps):
                    raise ValueError('官网当前预计滑点超过本次保护上限，未提交')
                if self.form.max_slippage_bps() > decimal(slippage_bps):
                    raise ValueError('官网滑点上限宽于策略限制，未提交')
                label = self.profile['submit_labels'][side]
                self.reader.arm({**quote, 'max_slippage_bps': slippage_bps}, side, size, reduce_only)
                self.form.verify(symbol, side, size, reduce_only, price=price)
                fresh_quote(current, symbol, size)
            except Exception as exc:
                result = {'success': False, 'confirmed': True, 'status': 'rejected',
                          'filled_size': 0, 'error': '点击前检查失败，未提交：'+safe_reason(exc)}
                self.journal.finish(row['seq'], result)
                return result
            try:
                with self._stage('V 提交按钮点击（非成交确认）',symbol):
                    self.form.click_submit(label)  # Never click a second time after ambiguity.
                with self._stage('V 成交回执等待',symbol):
                    receipt = self.reader.wait_terminal(timeout=10)
                result = verify_receipt(receipt, quote, side, size, reduce_only)
                if result['filled_size']:
                    actual = decimal(result['average_price'], True)
                    bound = decimal(price) * (1 + (decimal(slippage_bps)/10000 if side == 'buy' else -decimal(slippage_bps)/10000))
                    if (side == 'buy' and actual > bound) or (side == 'sell' and actual < bound):
                        raise ValueError('成交价越过保护范围，需核对')
            except Exception:
                result = unknown('点击或成交回执状态未确认；停止，不重试、不推定成交')
            self.journal.finish(row['seq'], result)
            return result

    def close_position(self, symbol, side, size, client_order_id, reduce_only=True, slippage_bps=10):
        if reduce_only is not True:
            raise ValueError('平仓必须只减仓')
        opposite = 'sell' if direction(side) == 'buy' else 'buy'
        quote = self.get_executable_quote(symbol, size)
        return self.place_order(symbol, opposite, size, quote['ask' if opposite == 'buy' else 'bid'],
                                slippage_bps, client_order_id, quote, reduce_only=True)
