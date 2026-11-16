"""Conservative, executable profit gates for discretionary single-pair exits."""
import math
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from contextlib import contextmanager

from src.common.market_data import age_seconds
from src.execution.position_monitor import PositionReadUnavailable
from src.execution.orders import fresh_quote, SubmissionWindowExpired
from src.common.market_data import utc_now
from src.execution.close_diagnostics import quote_snapshot
from src.strategy.position_routes import for_position


NORMAL_EXIT_REASONS = frozenset(('价差收敛', '盈利条件', '价差收敛且达到盈利'))


class ProfitExitMixin:
    @contextmanager
    def _exit_funding_guard(self, allow_wait):
        acquired = self._exit_funding_lock.acquire(blocking=allow_wait)
        if not acquired:
            raise PositionReadUnavailable('资金费快照正在后台更新；本次不等待网络读取')
        try:
            yield
        finally:
            self._exit_funding_lock.release()

    @staticmethod
    def _exit_has_fills(position):
        return any(float(row.get('filled_size') or 0) > 0
                   for row in position.close_order_results.values()) or any(
            getattr(position, 'entry_size_' + leg) > getattr(position, leg + '_size') + 1e-12
            for leg in ('variational', 'lighter'))

    def _defer_profit_exit(self, position, reason, estimate=None):
        self._record_close_diagnostic(position, reason, estimate, allowed=False)
        if self.position_monitor:
            self.position_monitor.checked(position, error=PositionReadUnavailable(reason))

    def _capture_close_signal(self, position, reason, observed_at, quotes, prices, **decision):
        """Memory only: freeze the exact reads already used by close_reason."""
        try:
            directional = self._directional_spread_bps(position, *prices)
            if position.entry_target:
                directional = self._target_convergence(position, quotes)[1]
            center = decision['center']
            self._close_signals[position.id] = dict(
                observed_at=observed_at, reason=reason, mode=self.close_mode,
                target_mode=(position.entry_target['target_mode'] if position.entry_target else self.close_target_mode),
                target_bps=position.entry_target['target_bps'] if position.entry_target else None,
                target_frozen_at=position.entry_target.get('frozen_at') if position.entry_target else None,
                edge_basis='target_return' if position.entry_target else 'opening_edge',
                effective_target=decision['effective_target'],
                basis=self.close_convergence_basis, tolerance_bps=self.close_convergence_bps,
                center_bps=center, center_samples=decision['stats'].get('samples'),
                center_window_seconds=(position.entry_target['window_seconds'] if position.entry_target else 7 * 86400),
                directional_spread_bps=directional,
                absolute_spread_bps=decision['current_spread_bps'],
                entry_spread_bps=decision['entry_spread_bps'], reduction_bps=decision['reduction_bps'],
                distance_to_center_bps=(abs(directional - float(center))
                                        if center is not None and directional is not None else None),
                convergence_ok=decision['convergence_ok'],
                legs={name: dict(quote_snapshot(quote), mid=price,
                    venue=self._venue_name(trader, name), position_side=getattr(position, name + '_side'),
                    size=getattr(position, name + '_size'))
                    for name, trader, quote, price in zip(('variational', 'lighter'),
                        (self.variational_trader, self.lighter_trader), quotes, prices)})
        except Exception:
            # Diagnostic failures must not suppress a valid trading decision.
            self._close_signals.pop(position.id, None)

    def _record_close_diagnostic(self, position, reason, estimate, allowed):
        """Bounded nonblocking enqueue; no platform read or disk access here."""
        try:
            estimate = estimate or {}
            check = dict(observed_at=utc_now(), allowed=allowed,
                net_pnl_usdc=estimate.get('net_pnl_usdc'), net_pnl_bps=estimate.get('net_pnl_bps'),
                reference_net_pnl_usdc=estimate.get('reference_net_pnl_usdc'),
                movement_cost_usdc=estimate.get('movement_cost_usdc'),
                closing_fee_usdc=estimate.get('closing_fee_usdc'),
                closing_fee_source=estimate.get('closing_fee_source'),
                entry_fees_usdc=position.entry_fees_usdc, funding_usdc=estimate.get('funding_usdc'),
                minimum_usdc=self.close_min_profit_usdc + self.close_cost_reserve_usdc,
                minimum_bps=self.close_min_profit_bps, legs={})
            if reason in ('最长持仓保护', '单笔亏损保护'):
                check.update(protective_exit=True,
                    held_seconds=(datetime.now() - position.open_time).total_seconds(),
                    max_hold_seconds=self.close_max_hold_seconds)
            for name, plan in estimate.get('legs', {}).items():
                check['legs'][name] = dict(quote_snapshot(plan['quote']),
                    venue=getattr(position, name + '_venue'), close_side=plan['side'],
                    size=plan['quote']['size'], reference_price=plan['reference_price'],
                    bound=plan['bound'], slippage_bps=plan['slippage_bps'])
            self.close_diagnostics.record(decision_id=uuid.uuid4().hex,
                position_id=position.id, execution_id=position.execution_id, symbol=position.symbol,
                reason=reason, trigger=self._close_signals.pop(position.id, None), pre_submit=check)
        except Exception:
            pass

    def _record_profit_wait(self, position, reason, estimate=None):
        """Rate-limited memory enqueue; optional diagnostics never block exits."""
        recent = getattr(self, '_profit_wait_recorded', {})
        key = (position.id, reason)
        now = time.monotonic()
        if now - recent.get(key, -10) < 5:
            self._close_signals.pop(position.id, None)
            return
        recent[key] = now
        self._profit_wait_recorded = {k: v for k, v in recent.items() if now - v < 60}
        self._record_close_diagnostic(position, reason, estimate, allowed=False)

    @staticmethod
    def _exit_fingerprint(position):
        return (position.id, position.variational_size, position.lighter_size,
                position.variational_side, position.lighter_side,
                position.entry_price_variational, position.entry_price_lighter,
                position.entry_fees_usdc, tuple(position.pending_fee_venues),
                position.variational_venue, position.lighter_venue)

    def _single_exit_funding(self, position, *, allow_refresh=True):
        """Bound private settlement reads independently from frequent WS quotes.

        Only complete, finite results up to five seconds old are usable. Cache
        failures too, so a missing settlement does not turn into a request storm.
        Funding is fetched before quotes so slow settlement reads cannot renew
        an old executable book's timestamp.
        """
        result = {}
        traders = (('variational', self.variational_trader), ('lighter', self.lighter_trader))
        with self._exit_funding_guard(allow_refresh):
            missing = {}
            for leg, trader in traders:
                key = (id(trader), position.symbol, position.open_time.isoformat())
                cached = self._exit_funding_cache.get(key)
                if not cached or time.monotonic()-cached[0] > 5:
                    missing[key] = trader
            def read(trader):
                observed = time.monotonic()
                reader = getattr(trader, 'reconcile_funding', None)
                try:
                    settlement = reader(position.symbol, position.open_time, datetime.now()) if callable(reader) else None
                except Exception:
                    settlement = None
                return time.monotonic(), settlement, observed
            if missing:
                if not allow_refresh:
                    raise PositionReadUnavailable('资金费快照待后台更新；本次不阻塞平仓发送')
                with ThreadPoolExecutor(max_workers=len(missing)) as pool:
                    futures = {key:pool.submit(read,trader) for key,trader in missing.items()}
                    for key,future in futures.items():
                        self._exit_funding_cache[key] = future.result()
            for leg, trader in traders:
                key = (id(trader), position.symbol, position.open_time.isoformat())
                cached = self._exit_funding_cache.get(key)
                settlement, observed = cached[1], cached[2]
                if (not isinstance(settlement, dict) or settlement.get('complete') is not True
                        or isinstance(settlement.get('funding_usdc'), bool)
                        or time.monotonic() - observed > 5):
                    raise PositionReadUnavailable('净盈利检查等待完整资金费记录')
                try:
                    amount = float(settlement['funding_usdc'])
                except (KeyError, TypeError, ValueError):
                    raise PositionReadUnavailable('净盈利检查等待完整资金费记录')
                if not math.isfinite(amount):
                    raise PositionReadUnavailable('资金费金额无效，暂不主动平仓')
                result[leg] = amount
            while len(self._exit_funding_cache) > 128:
                oldest = min(self._exit_funding_cache, key=lambda k:self._exit_funding_cache[k][0])
                self._exit_funding_cache.pop(oldest)
        return result

    def _single_monitor_exit_estimate(self, position):
        # Read-only monitoring needs no durable per-quote stage events. Order
        # intents, receipts and the final submission check keep their logging.
        return self._single_exit_estimate(position, record_stages=False)

    @for_position
    def _single_exit_estimate(self, position, *, record_stages=True):
        if (position.pending_fee_venues or isinstance(position.entry_fees_usdc, bool)
                or not math.isfinite(float(position.entry_fees_usdc))
                or position.entry_fees_usdc < 0):
            raise PositionReadUnavailable('净盈利检查等待已核验的开仓费用')
        funding = self._single_exit_funding(position, allow_refresh=(
            not record_stages or self.position_monitor is None))
        estimate = self._executable_exit_estimate(position, funding_by_leg=funding,
                                                 record_stages=record_stages)
        if estimate is None:
            raise PositionReadUnavailable('净盈利检查等待足量盘口、费率或执行预算')
        self._validate_single_exit_plan(position, estimate)
        # A later account check must not extend either quote beyond the
        # two-second execution window. Both transports enforce this deadline.
        remaining = min(2 - age_seconds(leg['quote']['updated_at']) for leg in estimate['legs'].values())
        deadline = time.monotonic() + max(0, remaining)
        for leg in estimate['legs'].values():
            leg['quote']['submit_deadline_monotonic'] = min(
                deadline, leg['quote']['submit_deadline_monotonic'])
        estimate['position_fingerprint'] = self._exit_fingerprint(position)
        if position.entry_target and position.entry_target.get('channel'):
            estimate['exit_policy'] = position.entry_target['channel'].get('close_policy',{})
        return estimate

    @staticmethod
    def _validate_single_exit_plan(position, estimate):
        legs = estimate.get('legs', {})
        if set(legs) != {'variational', 'lighter'}:
            raise PositionReadUnavailable('净盈利检查缺少双腿可成交报价')
        for name, leg in legs.items():
            try:
                fresh_quote(leg['quote'], position.symbol, getattr(position, name + '_size'))
            except SubmissionWindowExpired as exc:
                raise PositionReadUnavailable('平仓报价执行窗口已过期，等待新报价') from exc
            age = age_seconds(leg.get('quote', {}).get('updated_at'))
            if age is None or not 0 <= age <= 2:
                raise PositionReadUnavailable('平仓测算报价过期，等待重新核验净盈利')

    def _single_profit_ok(self, estimate):
        if not isinstance(estimate, dict):
            return False
        try:
            pnl, bps = float(estimate['net_pnl_usdc']), float(estimate['net_pnl_bps'])
            policy = estimate.get('exit_policy',{})
            minimum = policy.get('min_profit_usdc',self.close_min_profit_usdc) + policy.get('cost_reserve_usdc',self.close_cost_reserve_usdc)
            return (math.isfinite(pnl) and math.isfinite(bps) and pnl > 0
                    and pnl >= minimum and bps >= policy.get('min_profit_bps',self.close_min_profit_bps))
        except (KeyError, TypeError, ValueError):
            return False

    def _single_profit_estimate(self, position):
        monitor = self.position_monitor
        estimate = monitor.single_estimate(position) if monitor else self._single_exit_estimate(position)
        if estimate.get('position_fingerprint') != self._exit_fingerprint(position):
            raise PositionReadUnavailable('持仓或开仓费用已变化，等待重新核验净盈利')
        self._validate_single_exit_plan(position, estimate)
        return estimate
