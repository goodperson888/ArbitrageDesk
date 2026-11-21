"""Fail-closed hedge coordinator.

Live adapters must expose executable, size-bound quotes and terminal fill
receipts. Public prices and browser button clicks do not meet that contract.
"""
import json
import math
import os
import threading
import uuid
import copy
import time
from contextvars import copy_context
from contextlib import nullcontext
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from src.common.sizing import allocate, smaller_book_quantities, paired_depth_capacity, book_utilization_percent
from src.execution.telemetry import stage, safe_reason
from src.execution.history import TradeHistory
from src.execution.close_diagnostics import CloseDiagnostics, quote_snapshot
from src.execution.opportunity_observer import OpportunityObserver
from src.execution.hedge_policy import HedgePolicy, completion_limit, completion_edge, unwind_estimate
from src.execution.window_risk import ExecutionWindowRisk, REASON_LABELS, hedge_failures
from src.execution.exit_limits import CloseLiquidity
from src.common.market_data import age_seconds, utc_now
from src.api_v2.history import MultiVenueHistory
from src.strategy.spread_grid import SpreadGridMixin
from src.strategy.close_execution import CloseExecutionMixin, CloseRetryReads
from src.strategy.profit_exit import ProfitExitMixin, NORMAL_EXIT_REASONS
from src.strategy.target_return import TargetReturnMixin
from src.strategy.scan_diagnostics import rejection_kind, skipped
from src.execution.target_return import completion_target_limit, completion_return, directed_spread, projected_return
from src.execution.precheck import expected_entry_positions
from src.execution.preparation import readonly_preparation
from src.execution.position_recovery import position_fingerprint, resume_proof_valid
from src.strategy.position_routes import PositionRoutesMixin, for_position
from src.execution.entry_wait import entry_wait_scope, entry_result, entry_call
from src.strategy.entry_limits import EntryLimitsMixin


@dataclass
class ArbitrageOpportunity:
    symbol: str
    variational_price: float
    lighter_price: float
    spread_bps: float
    spread_direction: str
    expected_profit: float
    timestamp: datetime
    target_context: dict = None
    return_preview: dict = None
    # When more than two signed venues are selected, discovery annotates the
    # candidate with the concrete pair that produced it.  The execution worker
    # activates that pair immediately before the final re-quote/submit.
    route_index: int = None


@dataclass
class Position:
    symbol: str
    variational_side: str
    lighter_side: str
    variational_size: float
    lighter_size: float
    entry_price_variational: float
    entry_price_lighter: float
    open_time: datetime
    status: str = 'pending'
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    accounting_pending: bool = False
    pending_funding_venues: list = field(default_factory=list)
    pending_fee_venues: list = field(default_factory=list)
    funding_settlements: dict = field(default_factory=dict)
    # Keep original quantities and terminal marks separate from the mutable
    # remaining sizes.  Remaining sizes are reduced during a close, while the
    # execution history still needs the original exposure for review.
    entry_size_variational: float = 0
    entry_size_lighter: float = 0
    close_price_variational: float = None
    close_price_lighter: float = None
    closed_time: datetime = None
    realized_pnl_usdc: float = None
    entry_fees_usdc: float = 0.0
    execution_id: str = None
    execution_elapsed_ms: int = None
    close_elapsed_ms: int = None
    close_execution_ms: int = None
    close_accounting_ms: int = None
    close_started_time: datetime = None
    close_reason: str = None
    actual_entry_spread_bps: float = None
    variational_venue: str = None
    lighter_venue: str = None
    entry_rollback_recorded: bool = False
    rollback_category: str = None
    grid_cycle_id: str = None
    grid_layer: int = None
    grid_anchor_bps: float = None
    grid_entry_net_bps: float = None
    grid_direction: str = None
    close_attempts: dict = field(default_factory=dict)
    close_order_results: dict = field(default_factory=dict)
    close_receipt_order_ids: dict = field(default_factory=dict)
    resumed_at: str = None
    resume_count: int = 0
    entry_target: dict = None
    entry_target_projection: dict = None
    target_entry_net_bps: float = None
    entry_accounting: dict = field(default_factory=dict)
    manual_close: dict = field(default_factory=dict)


class ArbitrageStrategy(EntryLimitsMixin, PositionRoutesMixin, TargetReturnMixin, CloseExecutionMixin, ProfitExitMixin, SpreadGridMixin):
    def __init__(self, config, variational_trader, lighter_trader):
        self.config = config
        self.variational_trader = variational_trader
        self.lighter_trader = lighter_trader
        self.positions = []
        self.daily_trades = 0
        self.daily_pnl = 0.0
        self.last_trade_time = None
        self.day = datetime.now().date().isoformat()
        self.halted_reason = None
        self.entry_scan_backoff_until = None
        self.entry_scan_backoff_reason = None
        self._entry_balance_backoff = {}
        self._entry_scan_lock = threading.Lock()
        self._entry_preview_skips = 0
        self._last_entry_preview_skip = None
        self._last_cached_scan = None
        self.last_execution_submitted = False
        self.lock = threading.RLock()
        self._exit_funding_lock = threading.Lock()
        self._exit_funding_cache = {}
        self.state_path = Path(config.get('state_path', 'data/hedge_state.json'))
        self.config_path = Path(config['config_path']) if config.get('config_path') else None
        self._config_mtime_ns = self._read_config_mtime()
        self.history = TradeHistory(self.state_path.resolve().parent / 'hedge_events.sqlite')
        self.close_diagnostics = CloseDiagnostics(self.state_path.resolve().parent / 'close_diagnostics.sqlite')
        self._close_signals = {}
        self.close_liquidity = CloseLiquidity()
        from src.execution.precheck import EntryPrecheckMixin
        self.parallel_discovery = all(isinstance(trader, EntryPrecheckMixin)
                                      for trader in (variational_trader, lighter_trader))
        if self.parallel_discovery:
            for trader in (variational_trader, lighter_trader):
                trader.execution_history = self.history
        self.market_history = MultiVenueHistory(self.state_path.resolve().parent / 'multi_venue_history.sqlite')
        self._market_stats_cache = {}
        self._last_position_reconcile_at = 0.0
        self._last_position_reconcile_result = None
        self._entry_position_settling = {}
        self.position_monitor = None
        self.entry_wait_callback = None
        self._route_trader_pairs = []
        self._entry_route_indices = None
        self.runtime_scope = None
        self._active_route_index = 0
        self.position_size = config.get('trading', {}).get('position_size', 100)
        if config.get('execution_notional_cap') is not None:
            self.position_size = min(self.position_size, config['execution_notional_cap'])
        self.max_positions = config.get('trading', {}).get('max_positions', 1)
        self.max_total_position_notional = float(
            config.get('trading', {}).get('max_total_position_notional', 0) or 0)
        self.target_leverage = config.get('trading', {}).get('leverage')
        self.second_leg_retries = int(config.get('trading', {}).get('second_leg_retries', 1))
        # Only explicit enforce_common changes account leverage. Informational
        # reads in verify_only must not hold up equal-quantity entry orders.
        self.leverage_mode = config.get('trading', {}).get('leverage_mode', 'verify_only')
        self.max_daily_loss = config.get('risk', {}).get('max_daily_loss', 200)
        self.max_loss_per_trade = config.get('risk', {}).get('max_loss_per_trade', 50)
        self.slippage_tolerance = config.get('risk', {}).get('slippage_tolerance', 10)
        self.book_utilization_percent = book_utilization_percent(config.get('risk', {}).get('book_utilization_percent', 80))
        self.execution_safety_margin_bps = max(0.0, float(config.get('risk', {}).get(
            'entry_extra_profit_bps', config.get('risk', {}).get('execution_safety_margin_bps', 0))))
        self.spread_threshold_min = config.get('spread_threshold', {}).get('min', 5)
        self.spread_threshold_max = config.get('spread_threshold', {}).get('max', 50)
        entry_policy = config.get('entry_policy', {}) or {}
        self.entry_mode = entry_policy.get('mode', 'fixed')
        self.entry_quantile = float(entry_policy.get('quantile', 90))
        self.entry_min_samples = int(entry_policy.get('min_samples', 300))
        filters = config.get('execution_filters', {}) or {}
        self.gross_spread_filter_min_bps = float(filters.get('gross_min_bps', 0) or 0)
        configured_gross_max = filters.get('gross_max_bps')
        self.gross_spread_filter_max_bps = (float(configured_gross_max)
                                            if configured_gross_max not in (None, '', 0) else None)
        self.reversion_target_mode = filters.get('reversion_target_mode', 'center')
        self.reversion_window_seconds = int(filters.get('reversion_window_seconds', 604800) or 604800)
        self.reversion_min_count = int(filters.get('reversion_min_count', 0) or 0)
        self.reversion_min_deviation_bps = float(filters.get('reversion_min_deviation_bps', 0) or 0)
        self.reversion_max_age_seconds = float(filters.get('reversion_max_age_seconds', 0) or 0)
        self.reversion_recency_mode = 'soft' if filters.get('reversion_recency_mode') == 'soft' else 'hard'
        self.reversion_min_samples = int(filters.get('reversion_min_samples', 0) or 0)
        settings = config.get('wash_trading', {})
        self.max_trades_per_day = settings.get('max_trades_per_day', 100)
        self.trade_interval_min = settings.get('trade_interval_min', 60)
        self.reload_close_policy()
        self._reload_single_leg_recovery_policy()
        self.hedge_policy = HedgePolicy.from_config(config.get('risk', {}) or {})
        self._load_position_policy()
        self.route_rollback_guards = {}
        self.execution_window_risk = ExecutionWindowRisk()
        self.opportunity_observer = OpportunityObserver(
            self.state_path.resolve().parent / 'entry_observations.sqlite', self.close_diagnostics.writer)
        self._single_leg_recovery_attempts = {}
        self._load()

    @property
    def multi_route_enabled(self):
        return len(self._route_trader_pairs) > 1

    def configure_routes(self, pairs):
        """Register all read-only-approved signed venue pairs for this run.

        A strategy still executes exactly two legs.  With three or more
        selected venues, discovery evaluates every approved pair and the
        chosen pair is frozen when its first leg is submitted.
        """
        normalized = []
        for pair in pairs or []:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                continue
            first, second = pair
            if first is None or second is None or first is second:
                continue
            item = (first, second)
            if not any(item[0] is old[0] and item[1] is old[1] for old in normalized):
                normalized.append(item)
        if not normalized:
            normalized = [(self.variational_trader, self.lighter_trader)]
        self._route_trader_pairs = normalized
        # Resume an existing hedge on the route recorded with that position;
        # otherwise use the first approved route as the initial discovery pair.
        selected = 0
        active = next((p for p in self.positions if p.status != 'closed'), None)
        if active is not None:
            names = (getattr(active, 'variational_venue', None),
                     getattr(active, 'lighter_venue', None))
            for index, pair in enumerate(normalized):
                pair_names = (self._venue_name(pair[0], ''), self._venue_name(pair[1], ''))
                if names == pair_names:
                    selected = index
                    break
        self.activate_route(selected)

    def activate_route(self, index):
        try:
            index = int(index)
        except (TypeError, ValueError):
            raise ValueError('执行路线无效')
        if not self._route_trader_pairs:
            return
        if index < 0 or index >= len(self._route_trader_pairs):
            raise ValueError('执行路线无效')
        first, second = self._route_trader_pairs[index]
        self.variational_trader, self.lighter_trader = first, second
        self._active_route_index = index

    def set_entry_routes(self, pairs):
        """Retain immutable adapter identities for old positions and late probes."""
        from src.execution.precheck import EntryPrecheckMixin
        registry = list(self._route_trader_pairs or [(self.variational_trader, self.lighter_trader)])
        allowed = []
        for pair in pairs:
            index = next((i for i, old in enumerate(registry)
                          if tuple(old) == tuple(pair)), None)
            if index is None:
                index = len(registry)
                registry.append(tuple(pair))
            allowed.append(index)
            for trader in pair:
                trader.execution_history = self.history
        if not allowed:
            raise ValueError('没有通过核验的新仓路线')
        self._route_trader_pairs = registry
        self._entry_route_indices = frozenset(allowed)
        self.activate_route(allowed[0])
        self.parallel_discovery = all(isinstance(t, EntryPrecheckMixin) for pair in pairs for t in pair)

    def entry_route_allowed(self, index):
        if self._entry_route_indices is not None and index not in self._entry_route_indices:
            return False
        return self._entry_balance_backoff.get(index, 0) <= time.monotonic()

    def activate_route_for_opportunity(self, opportunity):
        index = getattr(opportunity, 'route_index', None)
        if index is not None:
            self.activate_route(index)

    def _read_config_mtime(self):
        if self.config_path is None:
            return None
        try:
            return self.config_path.stat().st_mtime_ns
        except OSError:
            return None

    def reload_runtime_config(self):
        """Reload entry capacity between attempts; other policy waits for flat."""
        if self.config_path is None:
            return False
        current_mtime = self._read_config_mtime()
        if current_mtime is None or current_mtime == self._config_mtime_ns:
            return False
        if (current_mtime == getattr(self, '_entry_limits_mtime_ns', None) and
                any(p.status != 'closed' for p in self.positions)):
            return False
        if current_mtime != getattr(self, '_saved_runtime_mtime_ns', None):
            import yaml
            try:
                with self.config_path.open(encoding='utf-8') as stream:
                    saved = yaml.safe_load(stream)
                loaded = saved['strategy']
                from src.strategy.entry_limits import validated_entry_limits
                validated_entry_limits(loaded)
            except (yaml.YAMLError, KeyError, AttributeError) as exc:
                raise ValueError('已保存策略配置无效') from exc
            self._saved_runtime_config = copy.deepcopy(loaded)
            self._saved_runtime_mtime_ns = current_mtime
        loaded = copy.deepcopy(self._saved_runtime_config)
        with self.lock:
            if any(p.status != 'closed' for p in self.positions):
                return self._reload_open_entry_limits(loaded, current_mtime)
        loaded['mode'] = self.config.get('mode', 'monitor')
        loaded['state_path'] = str(self.state_path)
        loaded['config_path'] = str(self.config_path)
        if 'execution_notional_cap' in self.config:
            loaded['execution_notional_cap'] = self.config['execution_notional_cap']
        if 'funding_accounting_mode' in self.config:
            loaded['funding_accounting_mode'] = self.config['funding_accounting_mode']
        if 'execution_filters' in self.config:
            loaded['execution_filters'] = copy.deepcopy(self.config['execution_filters'])
        self.config = loaded
        self._apply_entry_limits(loaded)
        self.target_leverage = loaded.get('trading', {}).get('leverage', self.target_leverage)
        self.second_leg_retries = int(loaded.get('trading', {}).get('second_leg_retries', self.second_leg_retries))
        self.leverage_mode = loaded.get('trading', {}).get('leverage_mode', self.leverage_mode)
        self.max_daily_loss = loaded.get('risk', {}).get('max_daily_loss', self.max_daily_loss)
        self.max_loss_per_trade = loaded.get('risk', {}).get('max_loss_per_trade', self.max_loss_per_trade)
        self.slippage_tolerance = loaded.get('risk', {}).get('slippage_tolerance', self.slippage_tolerance)
        self.book_utilization_percent = book_utilization_percent(loaded.get('risk', {}).get('book_utilization_percent', 80))
        self.execution_safety_margin_bps = max(0.0, float(loaded.get('risk', {}).get(
            'entry_extra_profit_bps', loaded.get('risk', {}).get('execution_safety_margin_bps', 0))))
        self.spread_threshold_min = loaded.get('spread_threshold', {}).get('min', self.spread_threshold_min)
        self.spread_threshold_max = loaded.get('spread_threshold', {}).get('max', self.spread_threshold_max)
        entry_policy = loaded.get('entry_policy', {}) or {}
        self.entry_mode = entry_policy.get('mode', self.entry_mode)
        self.entry_quantile = float(entry_policy.get('quantile', self.entry_quantile))
        self.entry_min_samples = int(entry_policy.get('min_samples', self.entry_min_samples))
        filters = loaded.get('execution_filters', {}) or {}
        self.gross_spread_filter_min_bps = float(filters.get('gross_min_bps', 0) or 0)
        configured_gross_max = filters.get('gross_max_bps')
        self.gross_spread_filter_max_bps = (float(configured_gross_max)
                                            if configured_gross_max not in (None, '', 0) else None)
        self.reversion_target_mode = filters.get('reversion_target_mode', self.reversion_target_mode)
        self.reversion_window_seconds = int(filters.get('reversion_window_seconds', self.reversion_window_seconds) or self.reversion_window_seconds)
        self.reversion_min_count = int(filters.get('reversion_min_count', self.reversion_min_count) or 0)
        self.reversion_min_deviation_bps = float(filters.get('reversion_min_deviation_bps', self.reversion_min_deviation_bps) or 0)
        self.reversion_max_age_seconds = float(filters.get('reversion_max_age_seconds', self.reversion_max_age_seconds) or 0)
        self.reversion_recency_mode = 'soft' if filters.get('reversion_recency_mode', self.reversion_recency_mode) == 'soft' else 'hard'
        self.reversion_min_samples = int(filters.get('reversion_min_samples', self.reversion_min_samples) or 0)
        wash = loaded.get('wash_trading', {}) or {}
        self.max_trades_per_day = wash.get('max_trades_per_day', self.max_trades_per_day)
        self.trade_interval_min = wash.get('trade_interval_min', self.trade_interval_min)
        self.reload_close_policy()
        self._reload_single_leg_recovery_policy()
        self.hedge_policy = HedgePolicy.from_config(loaded.get('risk', {}) or {})
        self._load_position_policy()
        self._config_mtime_ns = current_mtime
        self._entry_limits_mtime_ns = current_mtime
        self._pending_runtime_settings = False
        self.history.append('config_reloaded', reason='已保存配置在当前仓位平仓后生效')
        return True

    def resume_verified_position(self):
        """Consume a successful read-only recovery before a worker resumes.

        The recovery command writes this one-shot marker only after both
        selected accounts match the persisted open hedge.  It is consumed by
        the next signed worker and never authorizes a new entry by itself.
        """
        if not self.state_path.exists():
            return False
        try:
            data = json.loads(self.state_path.read_text())
        except (OSError, ValueError, TypeError):
            return False
        venues = [self._venue_name(self.variational_trader, 'variational'),
                  self._venue_name(self.lighter_trader, 'lighter')]
        if not resume_proof_valid(data, venues):
            return False
        if position_fingerprint([asdict(p) for p in self.positions]) != data['resume_proof']['fingerprint']:
            return False
        # Persist both the status transition and proof consumption together.
        # Clearing only halted_reason strands reconcile_required rows because
        # the normal close monitor intentionally manages open rows only.
        from src.common.market_data import utc_now
        resumed_at = utc_now()
        active = [p for p in self.positions if p.status != 'closed']
        for position in active:
            position.status = 'open'
            position.resumed_at = resumed_at
            position.resume_count = int(position.resume_count or 0) + 1
        rows_by_id = {p.id: p for p in active}
        for row in data['positions']:
            if row.get('id') in rows_by_id:
                position = rows_by_id[row['id']]
                row.update(status='open', resumed_at=resumed_at, resume_count=position.resume_count)
        self.halted_reason = None
        data['resume_verified'] = False
        data.pop('resume_proof', None)
        data['halted_reason'] = None
        self._atomic_state_payload(data)
        for position in active:
            self.history.append('position_resumed', symbol=position.symbol, position_id=position.id,
                                execution_id=position.execution_id,
                                reason='启动前两平台持仓与活动订单核对通过；已接管原仓位继续监控，未重发入场单')
        return True

    def _atomic_state_payload(self, payload):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix('.resume.tmp')
        with temporary.open('w') as out:
            json.dump(payload, out, default=lambda value: value.isoformat(),
                      ensure_ascii=False, allow_nan=False)
            out.flush(); os.fsync(out.fileno())
        os.replace(temporary, self.state_path)

    def reload_close_policy(self):
        """Load the user-facing close rules without changing open positions.

        The values are deliberately kept on the strategy instance because the
        close monitor runs in the worker and must use the same snapshot for one
        decision.  ``max_loss_per_trade`` and the maximum holding time remain
        safety exits regardless of the selected profit/convergence mode.
        """
        policy = self.config.get('close_policy', {}) or {}
        self.close_mode = policy.get('mode', 'convergence')
        self.close_target_mode = policy.get('target_mode', 'zero')
        self.close_convergence_basis = policy.get('convergence_basis', 'remaining')
        self.close_convergence_bps = float(policy.get('convergence_bps', 2))
        self.close_min_profit_usdc = float(policy.get('min_profit_usdc', 0))
        self.close_min_profit_bps = float(policy.get('min_profit_bps', 0))
        self.close_cost_reserve_usdc = float(policy.get('cost_reserve_usdc', 0))
        self.close_max_hold_seconds = int(policy.get('max_hold_seconds', 3600))

    def _reload_single_leg_recovery_policy(self):
        risk = self.config.get('risk', {}) or {}
        self.single_leg_recovery_enabled = bool(risk.get('single_leg_recovery_enabled', True))
        try:
            self.single_leg_recovery_max_attempts = max(1, int(risk.get('single_leg_recovery_max_attempts', 2)))
        except (TypeError, ValueError):
            self.single_leg_recovery_max_attempts = 2

    def gross_spread_allowed(self, buy_price, sell_price):
        try:
            spread = abs(float(sell_price) - float(buy_price)) / max(float(buy_price), float(sell_price)) * 10000
            return (spread >= self.gross_spread_filter_min_bps and
                    (self.gross_spread_filter_max_bps is None or spread <= self.gross_spread_filter_max_bps))
        except (TypeError, ValueError, ArithmeticError):
            return False

    def _route_guard_key(self, symbol, direction):
        return symbol.upper() + '|' + self._route_key_for_direction(direction)

    def execution_quality_rank(self, opportunity):
        # Resolve immutable route identities; background planning must not
        # switch the live coordinator's active adapters just for ranking.
        index = getattr(opportunity, 'route_index', None)
        pair = (self._route_trader_pairs[index] if index is not None and self._route_trader_pairs
                else (self.variational_trader, self.lighter_trader))
        venues = [self._venue_name(trader, name) for trader, name in
                  zip(pair, ('variational', 'lighter'))]
        if opportunity.spread_direction == 'variational_higher':
            venues.reverse()
        key = opportunity.symbol.upper() + '|' + '->'.join(venues)
        return self.execution_window_risk.quality(key)

    def _route_pause_reason(self, symbol, direction):
        item = self.route_rollback_guards.get(self._route_guard_key(symbol, direction), {})
        until = float(item.get('paused_until') or 0)
        if until > time.time():
            return ('该路线连续回补已暂停新开仓，恢复时间 ' +
                    datetime.fromtimestamp(until).strftime('%Y-%m-%d %H:%M:%S'))
        return None

    def _record_route_rollback(self, position):
        if (position.entry_rollback_recorded or not position.entry_size_variational or
                abs(position.entry_size_variational - position.entry_size_lighter) <= 1e-12):
            return
        direction = 'sell_variational' if position.variational_side in ('short', 'sell') else 'buy_variational'
        key = self._route_guard_key(position.symbol, direction)
        now = time.time()
        old = self.route_rollback_guards.get(key, {})
        expired = old.get('paused_until', 0) and old['paused_until'] <= now
        count = (0 if expired else int(old.get('rollback_count', 0))) + 1
        until = now + self.hedge_policy.cooldown_seconds if count >= self.hedge_policy.rollback_limit else 0
        self.route_rollback_guards[key] = {'rollback_count': count, 'paused_until': until,
                                          'last_failure_at': now, 'position_id': position.id,
                                          'reason_code': position.rollback_category or 'unknown'}
        self.execution_window_risk.outcome(key, position.rollback_category,
            position.entry_size_variational * position.entry_price_variational, attempt_id=position.id)
        self.opportunity_observer.outcome(position.id, 'rollback', reason_code=position.rollback_category,
            first_fill_price=position.entry_price_variational, first_fill_quantity=position.entry_size_variational,
            second_fill_quantity=position.entry_size_lighter)
        position.entry_rollback_recorded = True
        self._save()
        self.history.append('route_rollback_paused' if until else 'route_rollback_recorded',
                            symbol=position.symbol, position_id=position.id, route=key,
                            rollback_count=count, paused_until=until or None,
                            reason_code=position.rollback_category or 'unknown',
                            reason=(self._route_pause_reason(position.symbol, direction) if until
                                    else '本轮未能配成双腿，已记录路线回补次数'))

    def _reset_route_rollbacks(self, symbol, direction):
        key = self._route_guard_key(symbol, direction)
        self.route_rollback_guards.pop(key, None)

    def _restore_route_guards(self, data):
        guards = data.get('route_rollback_guards')
        if guards is not None:
            if not isinstance(guards, dict):
                raise ValueError('路线保护记录无效')
            for key, item in guards.items():
                count, until = int(item['rollback_count']), float(item['paused_until'])
                if count < 0 or not math.isfinite(until) or until < 0:
                    raise ValueError('路线保护记录无效')
                self.route_rollback_guards[key] = dict(item, rollback_count=count, paused_until=until)
            return
        # Older state files have no counters. Recover consecutive outcomes
        # in memory without writing to a running worker's state file.
        for position in sorted(self.positions, key=lambda p: p.open_time):
            if not position.entry_size_variational:
                continue
            direction = 'sell_variational' if position.variational_side in ('short', 'sell') else 'buy_variational'
            key = self._route_guard_key(position.symbol, direction)
            if position.entry_size_variational == position.entry_size_lighter:
                self.route_rollback_guards.pop(key, None)
            elif position.close_reason == '第二腿未成对·回补第一腿':
                at = (position.closed_time or position.open_time).timestamp()
                old = self.route_rollback_guards.get(key, {})
                expired = old.get('paused_until', 0) and old['paused_until'] <= at
                count = (0 if expired else int(old.get('rollback_count', 0))) + 1
                self.route_rollback_guards[key] = {
                    'rollback_count': count, 'paused_until':
                        at + self.hedge_policy.cooldown_seconds if count >= self.hedge_policy.rollback_limit else 0,
                    'last_failure_at': at, 'position_id': position.id}
                position.entry_rollback_recorded = True

    def execution_min_bps(self, symbol=None, direction=None):
        """Profit requirement only; price movement is budgeted separately."""
        return float(self.spread_threshold_min) + float(self.execution_safety_margin_bps)

    def execution_entry_movement_bps(self, symbol=None, direction=None):
        """Use the configured order budget; history is observational only.

        Entry uncertainty is covered by the user's entry reserve. Historical
        windows must not autonomously enlarge order guards or modeled closing
        costs, even when a previous attempt failed.
        """
        return min(self.hedge_policy.price_movement_bps, float(self.slippage_tolerance))

    def execution_movement_bps(self, symbol=None, direction=None):
        """Keep the existing closing-cost estimate independent of entry learning."""
        budget = self.hedge_policy.price_movement_bps
        if symbol:
            for route_direction in ([direction] if direction else ['buy_variational', 'sell_variational']):
                try:
                    stats = self._route_reversion_stats(symbol, self._route_key_for_direction(route_direction), 'center')
                    if int(stats.get('samples') or 0) >= 30 and stats.get('p10_bps') is not None and stats.get('p90_bps') is not None:
                        budget = max(budget, min(5.0, max(0.0, float(stats['p90_bps']) - float(stats['p10_bps'])) * .10))
                except (AttributeError, TypeError, ValueError, ArithmeticError):
                    continue
        return min(budget, float(self.slippage_tolerance))

    @staticmethod
    def _history_symbol(symbol):
        value = str(symbol or '').split('-', 1)[0].upper()
        return value + '-PERP' if value else str(symbol or '')

    def _route_key_for_direction(self, direction):
        first = self._venue_name(self.variational_trader, 'variational')
        second = self._venue_name(self.lighter_trader, 'lighter')
        return f'{first}->{second}' if direction == 'buy_variational' else f'{second}->{first}'

    def _route_reversion_stats(self, symbol, pair_key, target_mode='center'):
        from src.api_v2.center_statistics import reversion_stats_for
        if callable(getattr(type(self.market_history), 'shared_reversion_stats', None)):
            return reversion_stats_for(self.market_history, self._history_symbol(symbol),
                                       pair_key, 7 * 86400, 2.0, target_mode)
        cache_key = (self._history_symbol(symbol), pair_key, target_mode)
        cached = self._market_stats_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 30:
            return cached[1]
        stats = self.market_history.reversion_stats(
            self._history_symbol(symbol), pair_key, 7 * 86400, 2.0, target_mode)
        self._market_stats_cache[cache_key] = (time.monotonic(), stats)
        if len(self._market_stats_cache) > 2048:
            oldest = min(self._market_stats_cache, key=lambda key: self._market_stats_cache[key][0])
            self._market_stats_cache.pop(oldest, None)
        return stats

    def reversion_rank(self, opportunity):
        """Return the same centre-deviation ranking used by the scanner.

        Discovery prices are intentionally used only for ordering.  The
        quantity-bound ``plan_entry`` call still re-quotes both venues and
        applies the final net-edge, depth and account checks before submit.
        """
        if self.target_return_enabled and opportunity.target_context:
            return self.target_discovery_rank(opportunity)
        try:
            direction = ('sell_variational'
                         if opportunity.spread_direction == 'variational_higher'
                         else 'buy_variational')
            pair_key = self._route_key_for_direction(direction)
            stats = self._route_reversion_stats(opportunity.symbol, pair_key, 'center')
            samples = int(stats.get('samples') or 0)
            center = stats.get('center_bps')
            recency = self.reversion_recency_rank(opportunity.symbol, direction)
            current = float(opportunity.spread_bps)
            cap = self.entry_notional_cap()
            if center is None or samples < 30 or not math.isfinite(current) or not math.isfinite(cap) or cap <= 0:
                return (1, current, 0.0, recency, samples, pair_key)
            deviation = current - float(center)
            estimate = max(0.0, deviation) * cap / 10000.0
            if estimate > 0:
                return (2, estimate, current, recency, samples, pair_key)
            return (1, current, 0.0, recency, samples, pair_key)
        except (AttributeError, TypeError, ValueError, ArithmeticError):
            try:
                return (1, float(opportunity.spread_bps), 0.0, 0.0, 0, None)
            except (AttributeError, TypeError, ValueError):
                return (1, 0.0, 0.0, 0.0, 0, None)

    def _reversion_filter_stats(self, symbol, route_direction):
        from src.api_v2.center_statistics import reversion_stats_for
        return reversion_stats_for(self.market_history,
            self._history_symbol(symbol), self._route_key_for_direction(route_direction),
            max(300, min(7776000, int(self.reversion_window_seconds))), 2.0,
            self.reversion_target_mode, self.reversion_min_deviation_bps)

    def reversion_recency_rank(self, symbol, route_direction):
        """Only break equal-economic ranks; old history never approves entry."""
        if getattr(self, 'reversion_recency_mode', 'hard') != 'soft' or self.reversion_max_age_seconds <= 0:
            return 0.0
        try:
            stats = self._reversion_filter_stats(symbol, route_direction)
            if stats.get('statistics_ready') is False:
                return 0.0
            age = stats.get('last_reversion_age_seconds')
            return 1.0 if age is not None and 0 <= float(age) <= self.reversion_max_age_seconds else 0.0
        except (AttributeError, TypeError, ValueError, ArithmeticError):
            return 0.0

    def _reversion_filter_allowed(self, symbol, route_direction):
        """Apply the dashboard's historical-reversion filters to execution.

        The opportunity page and the live worker must use the same directed
        route.  A symbol that passed in the UI is not enough: the worker may
        otherwise re-quote the opposite route and bypass the selected
        history/count/recency constraints.
        """
        active = (self.reversion_target_mode != 'center' or
                  self.reversion_min_count > 0 or
                  self.reversion_min_deviation_bps > 0 or
                  self.reversion_max_age_seconds > 0 or
                  self.reversion_min_samples > 0)
        if not active:
            return True
        try:
            stats = self._reversion_filter_stats(symbol, route_direction)
            if stats.get('statistics_ready') is False:
                return False
            if int(stats.get('samples') or 0) < self.reversion_min_samples:
                return False
            if int(stats.get('reversion_count') or 0) < self.reversion_min_count:
                return False
            if self.reversion_max_age_seconds > 0 and getattr(self, 'reversion_recency_mode', 'hard') == 'hard':
                age = stats.get('last_reversion_age_seconds')
                if age is None or float(age) > self.reversion_max_age_seconds:
                    return False
            return True
        except (AttributeError, TypeError, ValueError, ArithmeticError):
            return False

    @staticmethod
    def _position_paired_notional(position):
        """Return one hedge's counted notional without double-counting both legs.

        A fully hedged pair is counted by its smaller confirmed leg.  During a
        partial/ambiguous fill, count the known leg so an incomplete hedge
        cannot make the aggregate cap appear unused.
        """
        values = []
        for size, price in ((getattr(position, 'variational_size', 0),
                             getattr(position, 'entry_price_variational', 0)),
                            (getattr(position, 'lighter_size', 0),
                             getattr(position, 'entry_price_lighter', 0))):
            try:
                value = abs(float(size)) * abs(float(price))
                if math.isfinite(value) and value > 0:
                    values.append(value)
            except (TypeError, ValueError, ArithmeticError):
                continue
        if not values:
            return 0.0
        return min(values) if len(values) > 1 else values[0]

    def open_position_notional(self):
        """Return aggregate counted notional for all non-closed positions."""
        return sum(self._position_paired_notional(position)
                   for position in self.positions if position.status != 'closed')

    def remaining_open_notional(self):
        """Return room under the optional aggregate open-notional cap."""
        try:
            limit = float(self.max_total_position_notional or 0)
        except (TypeError, ValueError):
            limit = 0.0
        if not math.isfinite(limit) or limit <= 0:
            return None
        return max(0.0, limit - self.open_position_notional())

    def entry_notional_cap(self):
        """Return the effective per-entry cap after aggregate exposure room."""
        try:
            cap = float(self.position_size)
        except (TypeError, ValueError):
            return 0.0
        remaining = self.remaining_open_notional()
        if remaining is not None:
            cap = min(cap, remaining)
        return max(0.0, min(cap, self._grid_cap()))

    def _adaptive_entry_gate(self, symbol, direction, buy, sell):
        if self.entry_mode != 'adaptive_quantile':
            return None
        pair_key = self._route_key_for_direction(direction)
        stats = (self._target_policy_stats(symbol, direction) if self.target_return_enabled
                 else self._route_reversion_stats(symbol, pair_key, 'center'))
        if stats.get('statistics_ready') is False:
            raise ValueError('历史分位统计正在后台更新，等待有效快照')
        samples = int(stats.get('samples') or 0)
        if samples < self.entry_min_samples:
            raise ValueError(f'历史样本不足以使用自适应分位入场: {samples}/{self.entry_min_samples}')
        current = (float(sell) - float(buy)) / max(float(buy), float(sell)) * 10000
        p10, p90 = ((stats.get('entry_lower_bps'), stats.get('entry_upper_bps')) if self.target_return_enabled
                    else (stats.get('p10_bps'), stats.get('p90_bps')))
        if p10 is None or p90 is None:
            raise ValueError('历史分位数据不足，等待下一轮样本')
        extreme = (current >= float(p90) if self.target_return_enabled
                   else current < float(p10) or current > float(p90))
        if extreme:
            return {'route': pair_key, 'current_bps': current, 'p10_bps': float(p10), 'p90_bps': float(p90), 'samples': samples}
        raise ValueError(f'当前价差未达到历史 P10/P90 入场区间: {current:.2f} bps，P10 {float(p10):.2f} / P90 {float(p90):.2f}')

    @staticmethod
    def _venue_name(trader, fallback):
        """Return the concrete adapter name used for execution telemetry.

        The strategy keeps the historical ``variational_trader`` and
        ``lighter_trader`` attribute names for compatibility, but generic
        pairs can now be Lighter mainnet + Lighter RH (or StandX).  Persisting
        those legacy names in receipts made a RH failure look like a
        Variational failure in the UI.
        """
        value = getattr(trader, 'venue', None)
        return str(value) if value else fallback

    @classmethod
    def _stage_venue_label(cls, trader, fallback):
        value = getattr(trader, 'venue', None)
        if not isinstance(value, str) or not value:
            return fallback
        return {
            'lighter': 'Lighter 主网',
            'lighter_rh': 'Lighter RH',
            'variational': 'Variational',
            'standx': 'StandX',
            'arcus': 'Arcus',
        }.get(value, value)

    def _load(self):
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text())
            self.daily_trades = data['daily_trades']
            self.daily_pnl = data['daily_pnl']
            self.day = data['day']
            self.last_trade_time = datetime.fromisoformat(data['last_trade_time']) if data.get('last_trade_time') else None
            self.halted_reason = data.get('halted_reason')
            self.runtime_scope = data.get('runtime_scope')
            # Reconciliation adds audit-only fields to persisted position
            # records (for example ``reconciled_at`` and ``reconciled_sizes``).
            # They are deliberately not part of the live Position model and
            # must not make an otherwise closed state look corrupt.  Keep the
            # loader forward-compatible while still validating the fields the
            # strategy actually uses.
            position_fields = {item.name for item in fields(Position)}
            for row in data['positions']:
                values = {key: value for key, value in row.items() if key in position_fields}
                values['open_time'] = datetime.fromisoformat(values['open_time'])
                if values.get('closed_time'):
                    values['closed_time'] = datetime.fromisoformat(values['closed_time'])
                if values.get('close_started_time'):
                    values['close_started_time'] = datetime.fromisoformat(values['close_started_time'])
                self.positions.append(Position(**values))
            self._restore_route_guards(data)
            self.execution_window_risk = ExecutionWindowRisk(data.get('execution_window_risk'))
            if any(p.status != 'closed' for p in self.positions):
                self.halted_reason = '重启发现未完成持仓；必须先与两边账户核对，禁止自动重下单'
        except Exception:
            self.halted_reason = '持仓日志损坏，必须人工核对账户'

    def _save(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix('.tmp')
        payload = {'positions': [asdict(p) for p in self.positions],
                   'daily_trades': self.daily_trades, 'daily_pnl': self.daily_pnl,
                   'last_trade_time': self.last_trade_time, 'day': self.day,
                   'halted_reason': self.halted_reason,
                   'position_policy': self.grid_status(),
                   'execution_window_risk': self.execution_window_risk.routes,
                   'route_rollback_guards': self.route_rollback_guards}
        if self.runtime_scope:
            payload['runtime_scope'] = self.runtime_scope
        with tmp.open('w') as out:
            json.dump(payload, out, default=lambda value: value.isoformat(), allow_nan=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, self.state_path)

    def live_blockers(self):
        reasons = []
        if self.channel_enabled:
            from src.execution.channel_policy import channel_policy
            try:
                channel_policy(self.config)
            except ValueError as exc:
                reasons.append(str(exc))
        if self.config.get('mode', 'monitor') != 'live':
            reasons.append('当前为行情监控模式')
        for trader in (self.variational_trader, self.lighter_trader):
            if getattr(trader, 'live_ready', False) is not True:
                reasons.append(getattr(trader, 'live_blocker', '交易适配器未通过实盘校验'))
        if self.halted_reason:
            reasons.append(self.halted_reason)
        if any(p.status not in ('open', 'closed') for p in self.positions):
            reasons.append('存在未核对或未接管持仓，禁止新开仓')
        if self.max_daily_loss is not None and any(p.pending_funding_venues for p in self.positions):
            reasons.append('资金费待补，无法核验已启用的每日净亏损限额')
        return reasons

    @staticmethod
    def _fill(result, requested):
        """Only terminal, explicitly confirmed receipts can establish exposure."""
        if not isinstance(result, dict) or result.get('confirmed') is not True:
            raise ValueError('订单成交状态未知')
        if result.get('status') not in ('filled', 'partial', 'rejected', 'canceled'):
            raise ValueError('订单仍在处理中')
        quantity = float(result['filled_size'])
        if not math.isfinite(quantity) or quantity < 0 or quantity > requested + 1e-12:
            raise ValueError('成交数量无效')
        if result['status'] in ('rejected',) and quantity != 0:
            raise ValueError('拒单回执与成交数量矛盾')
        if quantity > 0:
            price = float(result['average_price'])
            if not math.isfinite(price) or price <= 0 or not result.get('order_id'):
                raise ValueError('缺少成交价格或订单号')
        return quantity

    def _quotes(self, symbol, quantity, *, record_stages=True, sizing_books=None):
        from src.common.market_data import age_seconds
        first_label = self._stage_venue_label(self.variational_trader, 'V')
        second_label = self._stage_venue_label(self.lighter_trader, 'L')
        def fetch(name, trader, book=None):
            with stage(self.history,name,symbol) if record_stages else nullcontext():
                quote = (trader.quote_from_sizing_book(symbol, quantity, book) if book is not None
                         else trader.get_executable_quote(symbol, quantity))
            age = age_seconds(quote.get('updated_at'))
            if (quote.get('executable') is not True or quote.get('symbol') != symbol
                    or quote.get('instrument_type') != 'perpetual'
                    or age is None or age < 0 or age > 5
                    or Decimal(str(quote.get('size'))) != Decimal(str(quantity))):
                raise ValueError('报价非实时、非可执行或合约/数量不符')
            bid, ask = float(quote['bid']), float(quote['ask'])
            if not all(math.isfinite(p) and p > 0 for p in (bid, ask)) or bid > ask:
                raise ValueError('买卖价无效')
            return quote
        jobs = ((first_label + ' 数量报价', self.variational_trader),
                (second_label + ' 深度报价', self.lighter_trader))
        if sizing_books is not None:
            return [fetch(name, trader, book) for (name, trader), book in zip(jobs, sizing_books)]
        cached = {}
        for index, (name, trader) in enumerate(jobs):
            reader = getattr(type(trader), 'cached_sizing_book', None)
            if callable(reader):
                try:
                    book = reader(trader, symbol, quantity)
                    age = age_seconds(book.get('fetched_at')) if isinstance(book, dict) else None
                    if age is not None and 0 <= age <= .75:
                        cached[index] = fetch(name, trader, book)
                except (ValueError, TypeError, KeyError, ArithmeticError):
                    pass
        if len(cached) == 2:
            return [cached[0], cached[1]]
        # Both legs are read-only and independent at this point. Fetch them in
        # parallel so the second-leg quote has less time to move before the
        # first order is submitted. Each leg is still revalidated later.
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {i: pool.submit(copy_context().run, fetch, name, trader)
                       for i, (name, trader) in enumerate(jobs) if i not in cached}
            return [cached[i] if i in cached else entry_result(futures[i]) for i in range(2)]

    @staticmethod
    def _book_slippage_bps(book, side, quantity):
        levels = sorted(((float(row['price']), float(row['size'])) for row in book.get(side, [])),
                        reverse=side == 'bids')
        remaining, notional = float(quantity), 0.0
        if not levels or any(price <= 0 or size < 0 for price, size in levels):
            return None
        top = levels[0][0]
        for price, size in levels:
            take = min(remaining, size)
            notional += take * price
            remaining -= take
            if remaining <= 1e-12:
                vwap = notional / quantity
                # A crossed or unsorted venue snapshot can make VWAP look
                # better than the first level.  That is not negative
                # execution cost; clamp it to zero instead of sending a
                # negative budget into the sizing guard.
                return max(0.0, ((1 - vwap / top) if side == 'bids' else (vwap / top - 1)) * 10000)
        return None

    @staticmethod
    def _page_slippage_bps(quote):
        value = quote.get('page_estimated_slippage_bps')
        if type(value) not in (int, float) or not math.isfinite(value) or not -10000 <= value <= 10000:
            return None
        return max(0.0, value)

    def _venue_slippage_bps(self, trader, symbol, quantity, quote):
        """Return buy/sell impact for an API order-book venue.

        The first strategy attribute keeps the historical name
        ``variational_trader``, but a live pair can now be Lighter↔RH,
        Lighter↔StandX, or another two API venues.  Those adapters expose a
        quantity-bound book and must not inherit the web quote's fallback
        slippage budget.
        """
        if getattr(trader, 'requires_page_slippage', False) is True:
            value = self._page_slippage_bps(quote)
            return (value, value) if value is not None else (None, None)
        # Generic API adapters already return quantity-bound VWAPs with the
        # executable quote. Reuse those values instead of fetching the same
        # order book a second time during planning.
        try:
            bid, ask = float(quote['bid']), float(quote['ask'])
            bid_vwap, ask_vwap = float(quote['bid_vwap']), float(quote['ask_vwap'])
            if min(bid, ask, bid_vwap, ask_vwap) > 0 and bid <= ask:
                return (max(0.0, (ask_vwap / ask - 1) * 10000),
                        max(0.0, (1 - bid_vwap / bid) * 10000))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            pass
        get_book = getattr(trader, 'get_sizing_book', None)
        if not callable(get_book):
            return (None, None)
        book = get_book(symbol)
        from src.common.market_data import age_seconds
        age = age_seconds(book.get('fetched_at')) if isinstance(book, dict) else None
        if age is None or not 0 <= age <= 5:
            return (None, None)
        return (self._book_slippage_bps(book, 'asks', quantity),
                self._book_slippage_bps(book, 'bids', quantity))

    @staticmethod
    def _route_supports_symbol(pair, symbol):
        # Unknown catalogs retain the recovery path. A known missing market
        # must never consume REST slots or invalidate another venue pair.
        return all(not isinstance(scope := getattr(trader, 'discovery_supported_symbols', None),
                                  (set, frozenset)) or symbol in scope for trader in pair)

    def _check_arbitrage_opportunity_for_active_route(self, symbol):
        # Mark prices are only a discovery hint. Execution revalidates both quotes.
        if self.live_blockers() or not self._route_supports_symbol(
                (self.variational_trader, self.lighter_trader), symbol):
            return None
        def reference(name, trader):
            with stage(self.history,name,symbol):
                return trader.get_market_price(symbol)
        # Independent public reference reads can overlap. Quantity-bound quotes,
        # account checks and order submission remain explicitly ordered.
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_label = self._stage_venue_label(self.variational_trader, 'V')
            second_label = self._stage_venue_label(self.lighter_trader, 'L')
            v_future=pool.submit(reference,first_label + ' 参考价格',self.variational_trader)
            l_future=pool.submit(reference,second_label + ' 参考价格',self.lighter_trader)
            a,b=v_future.result(),l_future.result()
        return self._opportunity_from_references(symbol, a, b)

    def check_arbitrage_opportunity(self, symbol):
        if self.live_blockers():
            return None
        if not self.multi_route_enabled:
            return self._check_arbitrage_opportunity_for_active_route(symbol)
        return self.complete_reference_probe(self.probe_reference_prices(symbol))

    def probe_reference_prices(self, symbol):
        """Background reads never mutate the foreground execution route.

        Read each eligible venue once, including when it belongs to two pairs.
        A failure on one venue leaves the other complete pairs usable.
        """
        routes = [(index, pair) for index, pair in enumerate(self._route_trader_pairs)
                  if self.entry_route_allowed(index) and self._route_supports_symbol(pair, symbol)]
        traders = {id(trader): trader for _, pair in routes for trader in pair}
        def reference(trader):
            with stage(self.history, self._stage_venue_label(trader, '') + ' 参考价格', symbol):
                return trader.get_market_price(symbol)
        prices, errors = {}, []
        if traders:
            with ThreadPoolExecutor(max_workers=len(traders)) as pool:
                futures = {key: pool.submit(reference, trader) for key, trader in traders.items()}
                for key, future in futures.items():
                    try:
                        prices[key] = future.result()
                    except Exception as exc:
                        errors.append(exc)
                        self.record_entry_scan_failure(exc)
        result = [(index, prices[id(pair[0])], prices[id(pair[1])]) for index, pair in routes
                  if all(id(trader) in prices for trader in pair)]
        if not result and errors:
            raise errors[0]
        return dict(symbol=symbol, routes=result)

    def complete_reference_probe(self, probe):
        """Evaluate/rank a background probe only on the worker's main thread."""
        current = self._active_route_index
        candidates = []
        try:
            for index, first, second in probe['routes']:
                if not self.entry_route_allowed(index):
                    continue
                self.activate_route(index)
                if self.live_blockers():
                    continue
                opportunity = self._opportunity_from_references(probe['symbol'], first, second)
                if opportunity is not None:
                    opportunity.route_index = index
                    candidates.append(opportunity)
            if not candidates:
                return None
            def rank(opportunity):
                self.activate_route(getattr(opportunity, 'route_index', current))
                return self.reversion_rank(opportunity)
            return max(candidates, key=rank)
        finally:
            self.activate_route(current)

    def _cached_discovery_opportunities_for_active_route(self, symbols):
        """Evaluate cached quotes for the currently active two-leg route."""
        started = time.monotonic()
        candidates, missing, cached_count = [], [], 0
        diagnostics = dict(reasons={}, closest={}, directions=0)
        if not self.live_blockers():
            for symbol in symbols:
                if not self._route_supports_symbol((self.variational_trader, self.lighter_trader), symbol):
                    continue
                try:
                    quotes = [getattr(trader, 'cached_reference_quote', lambda s: None)(symbol)
                              for trader in (self.variational_trader, self.lighter_trader)]
                    if not all(isinstance(q, dict)
                               and (age := age_seconds(q.get('updated_at'))) is not None and 0 <= age <= 2
                               and type(q.get('bid')) in (int, float) and type(q.get('ask')) in (int, float)
                               and math.isfinite(q['bid']) and math.isfinite(q['ask'])
                               and 0 < q['bid'] <= q['ask'] for q in quotes):
                        missing.append(symbol)
                        diagnostics['directions'] += 2
                        diagnostics['reasons']['quotes'] = diagnostics['reasons'].get('quotes', 0) + 2
                        continue
                    cached_count += 1
                    a, b = [(q['bid'] + q['ask']) / 2 for q in quotes]
                    result = self._opportunity_from_references(symbol, a, b, quotes, scan=diagnostics)
                    if result is not None:
                        candidates.append(result)
                except Exception as exc:
                    self.record_entry_scan_failure(exc)
                    missing.append(symbol)
        with self._entry_scan_lock:
            self._last_cached_scan = dict(scope=len(symbols), cached_symbols=cached_count,
                missing_symbols=len(missing), candidates=len(candidates),
                elapsed_ms=round((time.monotonic()-started)*1000, 2), updated_at=utc_now())
            self._last_cached_scan['diagnostics'] = diagnostics
        return candidates, tuple(missing)

    def cached_discovery_opportunities(self, symbols):
        """Evaluate every selected venue pair before scheduling REST probes.

        A three-platform run used to start all feeds but instantiate the
        strategy with only the first two adapters.  This made Arcus appear in
        the UI while it never participated in live discovery.  Route scans are
        deliberately sequential because the strategy's quote/planning methods
        use the active pair; only the cached websocket reads happen here, so
        this remains bounded and avoids duplicate REST calls.
        """
        if not self.multi_route_enabled:
            return self._cached_discovery_opportunities_for_active_route(symbols)
        started = time.monotonic()
        all_candidates, all_missing = [], []
        route_stats = []
        diagnostics = dict(reasons={}, closest={}, directions=0)
        for index in range(len(self._route_trader_pairs)):
            if not self.entry_route_allowed(index):
                continue
            self.activate_route(index)
            candidates, missing = self._cached_discovery_opportunities_for_active_route(symbols)
            part = self._last_cached_scan.get('diagnostics', {})
            diagnostics['directions'] += part.get('directions', 0)
            for kind, count in part.get('reasons', {}).items():
                diagnostics['reasons'][kind] = diagnostics['reasons'].get(kind, 0) + count
            for kind, item in part.get('closest', {}).items():
                if kind not in diagnostics['closest'] or item['gap_bps'] < diagnostics['closest'][kind]['gap_bps']:
                    diagnostics['closest'][kind] = item
            for opportunity in candidates:
                opportunity.route_index = index
            all_candidates.extend(candidates)
            all_missing.append(set(missing))
            route_stats.append(dict(route_index=index, candidates=len(candidates),
                                    missing_symbols=len(missing)))
        # Probe missing eligible routes even if another pair already has
        # quotes for that symbol. Otherwise a failed third feed never recovers.
        missing = set().union(*all_missing)
        with self._entry_scan_lock:
            self._last_cached_scan = dict(
                scope=len(symbols),
                cached_symbols=len(set(symbols) - missing),
                missing_symbols=len(missing),
                candidates=len(all_candidates),
                routes=route_stats,
                elapsed_ms=round((time.monotonic() - started) * 1000, 2),
                updated_at=utc_now())
            self._last_cached_scan['diagnostics'] = diagnostics
        return all_candidates, tuple(sorted(missing))

    def _opportunity_from_references(self, symbol, a, b, cached=None, scan=None):
        if not all(isinstance(p, (float, int)) and math.isfinite(p) and p > 0 for p in (a, b)):
            return None
        if self.target_return_enabled:
            if cached is None:
                cached = [getattr(trader, 'cached_reference_quote', lambda s: None)(symbol)
                          for trader in (self.variational_trader, self.lighter_trader)]
            candidates = []
            for route_direction, buy, sell, direction in (
                    ('buy_variational', a, b, 'lighter_higher'),
                    ('sell_variational', b, a, 'variational_higher')):
                if scan is not None:
                    scan['directions'] += 1
                route = self._route_key_for_direction(route_direction)
                if self._route_pause_reason(symbol, route_direction) or not self._reversion_filter_allowed(symbol, route_direction):
                    skipped(scan, 'route_filter', symbol, route)
                    continue
                if cached and all(isinstance(q, dict) for q in cached):
                    buy_quote, sell_quote = cached if route_direction == 'buy_variational' else tuple(reversed(cached))
                    buy, sell = buy_quote['ask'], sell_quote['bid']
                spread = directed_spread(buy, sell)
                try:
                    context = self._entry_target_context(symbol, route_direction, spread, quotes=cached)
                except (ValueError, TypeError, ArithmeticError) as exc:
                    skipped(scan, getattr(exc, 'scan_kind', rejection_kind(str(exc))), symbol, route,
                            gap=getattr(exc, 'gap_bps', None), reason=str(exc))
                    continue
                floor = self.execution_min_bps(symbol, route_direction)
                if spread - context['target_bps'] < floor:
                    skipped(scan, 'net_return', symbol, route, gap=floor-(spread-context['target_bps']),
                            reason='未扣成本的回归空间已不足门槛')
                    continue
                preview = self.discovery_return_preview(symbol, route_direction, context, cached)
                if not self.discovery_preview_allowed(symbol, route_direction, preview):
                    skipped(scan, 'net_return', symbol, route,
                            gap=preview['optimistic_minimum_bps']-preview['optimistic_net_bps'],
                            net=preview.get('net_bps'), floor=preview.get('minimum_bps'),
                            reason='最乐观盘口收益仍未达门槛')
                    continue
                if scan is not None:
                    key = 'ready' if (preview or {}).get('meets_floor') else 'needs_validation'
                    scan['reasons'][key] = scan['reasons'].get(key, 0) + 1
                candidates.append(ArbitrageOpportunity(symbol, a, b, spread, direction, 0, datetime.now(), context, preview))
            return max(candidates, key=self.target_discovery_rank) if candidates else None
        # Keep discovery directional: compare the two possible buy/sell
        # routes and rank the favorable signed route.  The quantity-bound
        # quote and fee gate in plan_entry remains the final authority.
        if a >= b:
            buy, sell, direction = b, a, 'variational_higher'
        else:
            buy, sell, direction = a, b, 'lighter_higher'
        route_direction = 'sell_variational' if direction == 'variational_higher' else 'buy_variational'
        if self._route_pause_reason(symbol, route_direction):
            return None
        if not self._reversion_filter_allowed(symbol, route_direction):
            return None
        spread = (sell - buy) / max(sell, buy) * 10000
        return ArbitrageOpportunity(symbol, a, b, spread, direction,
                                    0, datetime.now())

    def _record_receipt(self, event, position, venue, receipt, requested_size=None, timing=None):
        timing = timing or {}
        self.history.append(event, symbol=position.symbol, position_id=position.id, venue=venue,
                            requested_size=requested_size, confirmed=receipt.get('confirmed'), submitted=receipt.get('submitted'),
                            execution_total_ms=self.history.current_elapsed_ms(),
                            reason=safe_reason(ValueError(receipt['error'])) if receipt.get('error') else None,
                            **{key: timing.get(key) for key in (
                                'first_submit_ms', 'first_receipt_processing_ms',
                                'first_receipt_to_second_quote_ms', 'second_quote_ms',
                                'second_decision_ms', 'second_submit_ms',
                                'first_receipt_to_second_send_ms') if timing.get(key) is not None},
                            **{k: receipt.get(k) for k in ('order_id', 'status', 'filled_size', 'average_price', 'fees_usdc', 'funding_usdc', 'fee_basis', 'fee_policy_url',
                                'raw_status', 'time_in_force', 'limit_price', 'platform_reason_code', 'platform_reason', 'platform_reason_reported',
                                'receipt_source', 'receipt_path', 'receipt_lookup_ms',
                                'receipt_ws_ready', 'receipt_rest_started', 'receipt_ws_grace_ms')})

    @staticmethod
    def _directional_spread_bps(position, price_variational, price_lighter):
        """Return the signed spread in the position's buy-to-sell direction."""
        try:
            if position.variational_side == 'long':
                buy, sell = float(price_variational), float(price_lighter)
            else:
                buy, sell = float(price_lighter), float(price_variational)
            if not all(math.isfinite(value) and value > 0 for value in (buy, sell)):
                return None
            return (sell - buy) / max(buy, sell) * 10000
        except (TypeError, ValueError, ArithmeticError):
            return None

    @staticmethod
    def _actual_entry_spread_bps(position):
        """Return the gross spread implied by the two confirmed entry fills."""
        return ArbitrageStrategy._directional_spread_bps(
            position, position.entry_price_variational, position.entry_price_lighter)

    def plan_entry(self, opportunity, fixed_quantity=None, fixed_direction=None, fixed_target=None,
                   allow_downsize=False):
        """Size-bound quotes; native depth is reused only within this call."""
        symbol = opportunity.symbol
        hard_entry_cap = self.entry_notional_cap()
        hint = fixed_direction or (('sell_variational' if opportunity.spread_direction == 'variational_higher' else 'buy_variational')
                                  if self.target_return_enabled else
                                  ('sell_variational' if opportunity.variational_price > opportunity.lighter_price else 'buy_variational'))
        learned_cap = self.execution_window_risk.size_cap(self._route_guard_key(symbol, hint))
        entry_cap = min(hard_entry_cap, learned_cap or hard_entry_cap)
        if entry_cap <= 0:
            raise ValueError('已达到最大总持仓金额上限')
        decimals = min(self.variational_trader.size_decimals(symbol), self.lighter_trader.size_decimals(symbol))
        step = Decimal(1).scaleb(-decimals)
        quantity = float((Decimal(str(entry_cap)) / Decimal(str(max(
            opportunity.variational_price, opportunity.lighter_price)))).quantize(step, rounding=ROUND_DOWN))
        if fixed_quantity is not None:
            quantity = float(fixed_quantity)
        if quantity <= 0:
            raise ValueError('上限低于数量精度')
        traders = (self.variational_trader, self.lighter_trader)
        sizing_books = None
        if all(callable(getattr(type(trader), 'quote_from_sizing_book', None)) for trader in traders):
            def read_book(trader, suffix):
                with stage(self.history, self._stage_venue_label(trader, '') + suffix, symbol):
                    cached = getattr(type(trader), 'cached_sizing_book', None)
                    result = cached(trader, symbol, quantity) if callable(cached) else None
                    result = result or trader.get_sizing_book(symbol)
                age = age_seconds(result.get('fetched_at'))
                if age is None or not 0 <= age <= 5:
                    raise ValueError('缩量盘口过期')
                return result
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(copy_context().run, read_book, trader, suffix)
                           for trader, suffix in zip(traders, (' 数量报价', ' 深度报价'))]
                sizing_books = [entry_result(future) for future in futures]
        # Only native books can be rebuilt locally at a smaller size. The
        # final caller must resize both account tickets before any submission.
        resize_allowed = fixed_quantity is None or (allow_downsize and sizing_books is not None)
        smaller = None
        def try_smaller():
            nonlocal quantity, smaller
            if sizing_books is None or not resize_allowed:
                return False
            if smaller is None:
                smaller = smaller_book_quantities(sizing_books, quantity, decimals)
            smaller = [q for q in smaller if q < quantity]
            if not smaller:
                return False
            quantity = smaller.pop(0)
            return True
        target_contexts = fixed_target
        constraints_cache = {}
        for _ in range(6):
            if sizing_books is not None:
                book = sizing_books[1]
            else:
                cached_book = getattr(type(self.lighter_trader), 'cached_sizing_book', None)
                book = (cached_book(self.lighter_trader, symbol, quantity) if callable(cached_book) else None)
                book = book or self.lighter_trader.get_sizing_book(symbol)
            age = age_seconds(book.get('fetched_at'))
            if age is None or not 0 <= age <= 5:
                raise ValueError('缩量盘口过期')
            # Both quote sides are required by the current adapter contract.
            visible = min(sum(float(x['size']) for x in book.get(side, [])) for side in ('bids', 'asks'))
            if sizing_books is not None:
                visible = min(visible, *(sum(float(x['size']) for x in sizing_books[0].get(side, []))
                                         for side in ('bids', 'asks')))
            if not resize_allowed and fixed_quantity is not None and Decimal(str(visible)) < Decimal(str(fixed_quantity)):
                raise ValueError('没有可配对的深度')
            quantity = float(Decimal(str(min(quantity, visible))).quantize(step, rounding=ROUND_DOWN))
            if quantity <= 0:
                raise ValueError('没有可配对的深度')
            v, l = self._quotes(symbol, quantity, **(
                {'sizing_books': sizing_books, 'record_stages': False} if sizing_books is not None else {}))
            # Reject impossible candidates before account precheck.  In
            # particular, Robinhood Chain can have a larger minimum base
            # amount than core Lighter (for example VVV requires 0.500 RH).
            # This keeps the UI from reporting a vague account failure when
            # the configured per-leg cap can never satisfy the venue rules.
            for label, trader, quote in (
                    (self._stage_venue_label(self.variational_trader, 'V'), self.variational_trader, v),
                    (self._stage_venue_label(self.lighter_trader, 'L'), self.lighter_trader, l)):
                constraints_fn = getattr(trader, 'order_constraints', None)
                if not callable(constraints_fn):
                    continue
                if sizing_books is not None and id(trader) in constraints_cache:
                    constraints = constraints_cache[id(trader)]
                else:
                    constraints = constraints_fn(symbol)
                    if sizing_books is not None:
                        constraints_cache[id(trader)] = constraints
                if not isinstance(constraints, dict):
                    continue
                try:
                    min_base = Decimal(str(constraints.get('min_base_amount')))
                    min_quote = Decimal(str(constraints.get('min_quote_amount')))
                    mark = (Decimal(str(quote['bid']))+Decimal(str(quote['ask'])))/2
                except (TypeError, ValueError, ArithmeticError):
                    continue
                q = Decimal(str(quantity))
                notional = q * mark
                if q < min_base or notional < min_quote:
                    raise ValueError(
                        f'{label} 最小下单条件不满足: 数量 {q:g} < {min_base:g} '
                        f'或名义金额 {notional:.6f} < {min_quote:g}')
            v_buy_slippage, v_sell_slippage = self._venue_slippage_bps(
                self.variational_trader, symbol, quantity, v)
            if v_buy_slippage is None or v_sell_slippage is None:
                if getattr(self.variational_trader, 'requires_page_slippage', False) is True:
                    raise ValueError('Variational 官网预计滑点缺失，等待新报价')
                raise ValueError('第一腿同数量盘口滑点缺失或过期，等待新报价')
            if (v_buy_slippage > self.slippage_tolerance or
                    v_sell_slippage > self.slippage_tolerance):
                if try_smaller():
                    continue
                raise ValueError('第一腿同数量盘口滑点超过保护上限，等待新报价')
            l_buy_slippage = self._book_slippage_bps(book, 'asks', quantity)
            l_sell_slippage = self._book_slippage_bps(book, 'bids', quantity)
            if (l_buy_slippage is None or l_sell_slippage is None or
                    l_buy_slippage > self.slippage_tolerance or l_sell_slippage > self.slippage_tolerance):
                if l_buy_slippage is not None and l_sell_slippage is not None and try_smaller():
                    continue
                raise ValueError('Lighter 同数量盘口滑点缺失或超过保护上限，等待新报价')
            movements = {d: self.execution_entry_movement_bps(symbol, d)
                         for d in ('buy_variational', 'sell_variational')}
            budget_ok = {
                # Identical-price decimal sizes can leave ~1e-12 bps of
                # floating-point residue; this is not a price movement.
                'buy_variational': max(v_buy_slippage, l_sell_slippage) + movements['buy_variational'] <= self.slippage_tolerance + 1e-9,
                'sell_variational': max(l_buy_slippage, v_sell_slippage) + movements['sell_variational'] <= self.slippage_tolerance + 1e-9,
            }
            if not any(ok and (fixed_direction is None or d == fixed_direction) for d, ok in budget_ok.items()):
                if try_smaller():
                    continue
                raise ValueError('盘口冲击加实际执行窗口价格预算超过每腿滑点上限，首腿未提交')
            slippage = {
                'buy_variational': {'buy': v_buy_slippage + min(movements['buy_variational'], self.slippage_tolerance), 'sell': l_sell_slippage + min(movements['buy_variational'], self.slippage_tolerance)},
                'sell_variational': {'buy': l_buy_slippage + min(movements['sell_variational'], self.slippage_tolerance), 'sell': v_sell_slippage + min(movements['sell_variational'], self.slippage_tolerance)},
            }
            models, target_errors, closing = {}, {}, {}
            if self.target_return_enabled:
                closing = {d: {'variational': v_sell_slippage + movements[d] if d == 'buy_variational' else v_buy_slippage + movements[d],
                               'lighter': l_buy_slippage + movements[d] if d == 'buy_variational' else l_sell_slippage + movements[d]}
                           for d in movements}
                for d in movements:
                    budget_ok[d] = budget_ok[d] and max(closing[d].values()) <= self.slippage_tolerance + 1e-9
                opening_impacts = {
                    'buy_variational': {'buy': v_buy_slippage, 'sell': l_sell_slippage},
                    'sell_variational': {'buy': l_buy_slippage, 'sell': v_sell_slippage},
                }
                models, target_errors = self._target_models(symbol, v, l, opening_impacts, closing, target_contexts)
                if not models:
                    raise ValueError(target_errors.get(fixed_direction or hint) or next(iter(target_errors.values()), '回归目标不可用'))
                if target_contexts is None:
                    target_contexts = {d: model['context'] for d, model in models.items()}
                edge_model = lambda d, buy, sell, qty: self._target_edge(models, d, buy, sell, qty)
            else:
                edge_model = None
            evaluated_edges = {}
            for direction, buy, sell, bf, sf in (
                    ('buy_variational',float(v['ask']),float(l['bid']),float(v['fee_bps']),float(l['fee_bps'])),
                    ('sell_variational',float(l['ask']),float(v['bid']),float(l['fee_bps']),float(v['fee_bps']))):
                buy_slip = slippage[direction]['buy'] / 10000
                sell_slip = slippage[direction]['sell'] / 10000
                v_slip = v_buy_slippage if direction == 'buy_variational' else v_sell_slippage
                l_slip = l_sell_slippage if direction == 'buy_variational' else l_buy_slippage
                gross=(sell-buy)/max(buy,sell)*10000
                protected=(sell*(1-sell_slip)*(1-sf/10000)-buy*(1+buy_slip)*(1+bf/10000))/max(buy,sell)*10000
                opening_edge = protected
                if edge_model is not None:
                    modeled = edge_model(direction, buy, sell, quantity)
                    protected, opening_edge = modeled['net_edge_bps'], modeled.get('opening_edge_bps', opening_edge)
                minimum = (self._target_floor_bps(quantity, max(buy, sell), models[direction]['projection'], reserve=True)
                           if direction in models else self.execution_min_bps(symbol))
                evaluated_edges[direction] = (protected, minimum)
                self.opportunity_observer.observe(self._route_guard_key(symbol, direction),
                    quantity, v, l, protected, minimum, gross)
                evidence = self.execution_window_risk.evidence(self._route_guard_key(symbol, direction))
                self.history.append('quote_evaluation',symbol=symbol,direction=direction,quantity=quantity,
                    discovery_gross_bps=opportunity.spread_bps,
                    gross_edge_bps=gross,net_edge_bps=protected,
                    price_movement_bps=movements[direction],
                    execution_window_samples=evidence['samples'], execution_window_ms=evidence['window_ms'],
                    projection_decay_bps=evidence['projection_decay_bps'],
                    recent_window_failures=evidence['recent_failures'],
                    entry_reserve_bps=self.execution_safety_margin_bps, base_min_edge_bps=self.spread_threshold_min,
                    buy_slippage_bps=slippage[direction]['buy'], sell_slippage_bps=slippage[direction]['sell'],
                    variational_slippage_bps=v_slip, lighter_slippage_bps=l_slip,
                    variational_slippage_source='Variational 官网当前预计值（页面）',
                    lighter_slippage_source='Lighter 同数量盘口 VWAP 相对首档',
                    slippage_breakdown=('买 V 页面预计滑点 + 卖 L 盘口冲击' if direction == 'buy_variational'
                                        else '买 L 盘口冲击 + 卖 V 页面预计滑点'),
                    slippage_budget_bps=slippage[direction]['buy'] + slippage[direction]['sell'],
                    min_edge_bps=minimum,
                    edge_basis='target_return' if edge_model is not None else 'opening_edge',
                    opening_edge_bps=opening_edge,
                    target_context=models.get(direction, {}).get('context'),
                    target_projection=models.get(direction, {}).get('projection'))
            allocation_edge = (lambda d, buy, sell, qty: self._target_edge(
                models, d, buy, sell, qty, second_depth_in_price=True)) if edge_model else None
            depth_limits, depth_capacities = None, {}
            if sizing_books is not None and self.book_utilization_percent < 100:
                depth_limits = {}
                for d in movements:
                    def level_allowed(buy, sell):
                        if d in models:
                            model = models[d]
                            net = projected_return(buy, sell, 1, model['buy_fee_bps'], model['sell_fee_bps'],
                                0, 0, model['projection'])['net_edge_bps']
                        else:
                            bf, sf = ((v['fee_bps'], l['fee_bps']) if d == 'buy_variational'
                                      else (l['fee_bps'], v['fee_bps']))
                            move = movements[d] / 10000
                            net = (sell * (1-move) * (1-sf/10000) -
                                   buy * (1+move) * (1+bf/10000)) / max(buy, sell) * 10000
                        return net > 0 and net >= self.execution_min_bps(symbol, d)
                    capacity = paired_depth_capacity(*sizing_books, d,
                        self.slippage_tolerance - movements[d], level_allowed)
                    depth_capacities[d] = capacity
                    depth_limits[d] = float((Decimal(str(capacity)) *
                        Decimal(str(self.book_utilization_percent)) / 100).quantize(step, rounding=ROUND_DOWN))
            plans = allocate(v, book, entry_cap, decimals, self.execution_min_bps(symbol),
                             self.spread_threshold_max, slippage,
                             l['fee_bps'], max_quantity=quantity, edge_model=allocation_edge,
                             direction_quantity_limits=depth_limits)
            valid = [p for p in plans if p['available'] and budget_ok[p['direction']] and
                     (fixed_direction is None or p['direction'] == fixed_direction) and
                     not self._route_pause_reason(symbol, p['direction'])]
            for plan in plans:
                direction = plan['direction']
                plan['variational_slippage_bps'] = (
                    v_buy_slippage if plan['direction'] == 'buy_variational' else v_sell_slippage)
                plan['lighter_slippage_bps'] = (l_sell_slippage if direction == 'buy_variational'
                                                 else l_buy_slippage)
                plan['slippage_breakdown'] = (
                    '买 V 页面预计滑点 + 卖 L 同数量盘口冲击'
                    if direction == 'buy_variational'
                    else '买 L 同数量盘口冲击 + 卖 V 页面预计滑点')
            if not valid:
                pauses = [self._route_pause_reason(symbol, plan['direction']) for plan in plans
                          if plan['available']]
                if any(pauses):
                    raise ValueError(next(reason for reason in pauses if reason))
                # A discovery spread above the configured ceiling is treated
                # as suspicious until re-quoted; it is not a depth failure.
                if any(plan.get('reason') == '整体净价差超过设置的最大阈值' for plan in plans):
                    raise ValueError('预计净回归空间超过策略上限，等待下一轮报价' if edge_model else '净价差超过策略上限，等待下一轮报价')
                if try_smaller():
                    continue
                checked_edges = [values for direction, values in evaluated_edges.items()
                                 if fixed_direction is None or direction == fixed_direction]
                if edge_model and checked_edges and all(net <= 0 or net < floor for net, floor in checked_edges):
                    raise ValueError('本次数量的预计整轮净收益未达入场门槛')
                raise ValueError('没有符合预计净回归空间与金额上限的深度' if edge_model else '没有符合净价差与金额上限的深度')
            best = max(valid, key=lambda p: (p['estimated_entry_edge_usdc'], p['net_edge_bps'],
                self.reversion_recency_rank(symbol, p['direction'])))
            learned_cap = self.execution_window_risk.size_cap(self._route_guard_key(symbol, best['direction']))
            route_cap = min(hard_entry_cap, learned_cap or hard_entry_cap)
            # The final pre-submit quote must keep its checked quantity. A
            # smaller initial candidate always obtains its own size-bound RFQ.
            max_price = max(float(v['bid']), float(v['ask']), float(l['bid']), float(l['ask']))
            if quantity * max_price > route_cap + 1e-10:
                if not resize_allowed:
                    raise ValueError('当前路线缩量上限已变化，首腿未提交')
                entry_cap = route_cap
                quantity = float((Decimal(str(route_cap)) / Decimal(str(max_price))).quantize(step, rounding=ROUND_DOWN))
                if quantity <= 0:
                    raise ValueError('回补后缩量低于数量精度，首腿未提交')
                continue
            if Decimal(str(best['quantity'])) != Decimal(str(quantity)):
                if not resize_allowed:
                    raise ValueError('重新报价后预计净回归空间不满足条件' if edge_model else '重新报价后净价差不满足条件')
                quantity = best['quantity']
                continue
            sell_v = best['direction'] == 'sell_variational'
            buy, sell = (float(l['ask']), float(v['bid'])) if sell_v else (float(v['ask']), float(l['bid']))
            bf, sf = (float(l['fee_bps']), float(v['fee_bps'])) if sell_v else (float(v['fee_bps']), float(l['fee_bps']))
            buy_slip = float(best['buy_slippage_bps']) / 10000
            sell_slip = float(best['sell_slippage_bps']) / 10000
            net = (sell * (1-sell_slip) * (1-sf/10000) - buy * (1+buy_slip) * (1+bf/10000)) / max(buy, sell) * 10000
            route_direction = 'sell_variational' if sell_v else 'buy_variational'
            floor = self.execution_min_bps(symbol, route_direction)
            if edge_model is not None:
                net = edge_model(route_direction, buy, sell, quantity)['net_edge_bps']
                floor = self._target_floor_bps(quantity, max(buy, sell), models[route_direction]['projection'], reserve=True)
                best.update(target_context=models[route_direction]['context'],
                            target_projection=models[route_direction]['projection'],
                            close_slippage_bps=closing[route_direction], edge_basis='target_return',
                            opening_edge_bps=edge_model(route_direction, buy, sell, quantity)['opening_edge_bps'])
            if not (net > 0 and floor <= net <= self.spread_threshold_max):
                raise ValueError('重新报价后预计净回归空间不满足条件' if edge_model else '重新报价后净价差不满足条件')
            grid_context = self._grid_context(symbol, route_direction)
            if grid_context and net + 1e-9 < grid_context['minimum_bps']:
                raise ValueError(f"未到下一网格：扣成本及预算后 {net:.2f} bps < {grid_context['minimum_bps']:.2f} bps")
            best['grid_context'] = grid_context
            if quantity * max(buy, sell) * (1+buy_slip) > entry_cap + 1e-10:
                raise ValueError('重新报价超过当前有效持仓金额上限')
            best.update(net_edge_bps=net, estimated_entry_edge_usdc=net / 10000 * max(buy, sell) * quantity)
            best['cap_usdc'] = entry_cap
            best['size_recovery_cap_usdc'] = hard_entry_cap
            best['price_movement_bps'] = movements[best['direction']]
            if depth_limits is not None:
                best.update(book_utilization_percent=self.book_utilization_percent,
                            paired_depth_quantity=depth_capacities[best['direction']],
                            depth_quantity_limit=depth_limits[best['direction']])
            adaptive = self._adaptive_entry_gate(symbol, best['direction'], buy, sell)
            if adaptive:
                best.update(entry_quantile=adaptive)
            return best, v, l, step
        raise ValueError('连续报价变化，跳过本次机会')

    def _ensure_common_leverage(self, symbol):
        """Verify leverage, optionally setting one value on both Lighter legs.

        Browser and third-party adapters do not expose a verified leverage
        setter yet, so this guard only activates when both legs support the
        same signed operation.  A mixed pair remains fail-closed instead of
        pretending that a config value changed the remote account.
        """
        target = self.target_leverage
        traders = (self.variational_trader, self.lighter_trader)
        if target is None:
            return []
        mode = self.leverage_mode
        if mode not in ('verify_only', 'enforce_common'):
            raise ValueError('杠杆模式无效')
        if mode == 'enforce_common':
            supported = [callable(getattr(trader, 'ensure_leverage', None)) for trader in traders]
            if not any(supported):
                return []
            if not all(supported):
                raise ValueError('统一杠杆尚未覆盖两条执行腿，已阻止开仓')
            results = [trader.ensure_leverage(symbol, target) for trader in traders]
            if any(abs(float(item['current_leverage']) - float(target)) > 1e-9 for item in results):
                raise ValueError('两条执行腿的实际杠杆未统一，已阻止开仓')
        else:
            # Read-only account/margin verification.  Different accounts may
            # legitimately use different leverage; margin checks still run
            # immediately before each order.
            info_fns = [getattr(trader, 'leverage_info', None) for trader in traders]
            info_fns = [fn for fn in info_fns if callable(fn)]
            if info_fns:
                with ThreadPoolExecutor(max_workers=len(info_fns)) as pool:
                    results = list(pool.map(lambda fn: fn(symbol), info_fns))
            else:
                results = []
            if not results:
                return []
        self.history.append('leverage_verified', symbol=symbol, target_leverage=float(target),
                            venues=[self._venue_name(trader, 'unknown') for trader in traders],
                            effective_leverages=[float(item['current_leverage']) for item in results],
                            leverage_setting_sources=[item.get('setting_source', 'account') for item in results],
                            changed=any(bool(item.get('changed')) for item in results),
                            mode=mode)
        return results

    @property
    def parallel_preparation(self):
        # Preparation calls use the strategy's active two-leg adapters.  Keep
        # them serial when candidates came from multiple routes so one route
        # cannot change the adapters while another route is being prepared.
        return self.parallel_discovery and self.target_return_enabled and not self.multi_route_enabled

    def _discovery_admission(self, symbol, *, preparation=False, pair=None):
        delay, hosts = 0.0, set()
        for trader in pair or (self.variational_trader, self.lighter_trader):
            # Only explicit adapter hooks; never start metadata/account reads
            # or discover an accidental mock attribute in this coordinator.
            if not callable(getattr(type(trader), 'discovery_admission', None)):
                continue
            admission = trader.discovery_admission(symbol, preparation=preparation)
            delay = max(delay, float(admission['wait_seconds']))
            hosts.update(admission['rest_hosts'])
        return {'wait_seconds': delay, 'rest_hosts': tuple(sorted(hosts))}

    def entry_preparation_admission(self, opportunity):
        return self._discovery_admission(opportunity.symbol, preparation=True)

    def reference_probe_wait_seconds(self, symbol):
        if self.multi_route_enabled:
            return min((self._discovery_admission(symbol, pair=pair)['wait_seconds']
                        for i, pair in enumerate(self._route_trader_pairs)
                        if self.entry_route_allowed(i) and self._route_supports_symbol(pair, symbol)),
                       default=0)
        return self._discovery_admission(symbol)['wait_seconds']

    def entry_preparation_key(self):
        """Changes in exposure, sizing or policy invalidate all prepared plans."""
        return (self.entry_notional_cap(), self.position_size, self.close_target_mode,
                json.dumps({k: self.config.get(k) for k in
                            ('trading', 'risk', 'entry_policy', 'close_policy', 'spread_threshold', 'position_policy', 'execution_filters')}, sort_keys=True),
                tuple((p.id, p.status, p.variational_size, p.lighter_size) for p in self.positions if p.status != 'closed'))

    def prepare_entry_candidate(self, opportunity):
        """Read-only sizing; deliberately excludes account settings and submits."""
        key = self.entry_preparation_key()
        with readonly_preparation(), self.history.buffered(), self.history.execution_scope(uuid.uuid4().hex[:16]):
            try:
                with stage(self.history, '生成双腿计划', opportunity.symbol):
                    plan = self.plan_entry(opportunity)
                return dict(opportunity=opportunity, plan=plan, prepared_at=time.monotonic(), key=key,
                            recency_rank=self.reversion_recency_rank(opportunity.symbol, plan[0]['direction']))
            except Exception as exc:
                with self._entry_scan_lock:
                    self._last_sized_rejection = dict(symbol=opportunity.symbol,
                        kind=rejection_kind(str(exc)), reason=safe_reason(exc), updated_at=utc_now())
                self.record_entry_scan_failure(exc)
                self.history.append('blocked', symbol=opportunity.symbol, stage='候选并行只读核验', reason=safe_reason(exc))
                return dict(opportunity=opportunity, error=True, prepared_at=time.monotonic(), key=key,
                            retry_after_seconds=max(getattr(exc, 'wait_seconds', 0),
                                                    getattr(exc, 'retry_after_seconds', 0)))

    def _prepared_plan(self, opportunity, prepared):
        if not prepared or prepared.get('opportunity') is not opportunity or prepared.get('error'):
            return None
        if time.monotonic()-prepared['prepared_at'] > 1 or prepared['key'] != self.entry_preparation_key():
            return None
        selected, first, second, step = prepared['plan']
        if any(age_seconds(q.get('updated_at')) is None or not 0 <= age_seconds(q['updated_at']) <= 2
               for q in (first, second)):
            return None
        return copy.deepcopy((selected, first, second, step))

    def execute_prepared_candidate(self, prepared):
        return self.execute_arbitrage(prepared['opportunity'], _prepared=prepared)

    def execute_arbitrage(self, opportunity, _retry_requoted=True, _prepared=None):
        # Keep every stage, receipt and terminal result from one attempt
        # together in the history.  This lets the UI show both the total
        # duration and the individual stage timings without guessing from
        # timestamps.
        execution_id = uuid.uuid4().hex[:16]
        watch = self.entry_wait_callback if self.parallel_discovery and any(
            p.status == 'open' and p.symbol != opportunity.symbol for p in self.positions) else None
        with self.history.execution_scope(execution_id), entry_wait_scope(watch, opportunity.symbol):
            with self.lock:
                self.last_execution_submitted = False
                try:
                    evidence = self._grid_entry_evidence(opportunity.symbol)
                except ValueError as exc:
                    self.history.append('blocked', symbol=opportunity.symbol, reason=str(exc))
                    return False
                with expected_entry_positions(evidence):
                    return self._execute_arbitrage(opportunity, _retry_requoted, _prepared)

    def _execute_arbitrage(self, opportunity, _retry_requoted=True, _prepared=None):
        with self.lock:
            self.last_execution_submitted = False
            if not self.entry_route_allowed(self._active_route_index):
                return False
            self.reset_daily_stats()
            if self.live_blockers() or (self.max_daily_loss is not None and self.daily_pnl <= -self.max_daily_loss):
                self.history.append('blocked', symbol=opportunity.symbol, reason='执行未就绪、待核对订单或每日亏损限额')
                return False
            if self.daily_trades >= self.max_trades_per_day:
                self.history.append('blocked', symbol=opportunity.symbol, reason='已达每日尝试次数上限')
                return False
            if len([p for p in self.positions if p.status != 'closed']) >= self.max_positions:
                self.history.append('blocked', symbol=opportunity.symbol, reason='已达同时持仓数上限')
                return False
            aggregate_limit = float(self.max_total_position_notional or 0)
            if aggregate_limit > 0 and self.entry_notional_cap() <= 0:
                self.history.append('blocked', symbol=opportunity.symbol,
                                    reason='已达最大总持仓金额上限',
                                    open_notional_usdc=self.open_position_notional(),
                                    max_total_position_notional=aggregate_limit)
                return False
            if not self.grid_enabled and any(p.symbol == opportunity.symbol and p.status != 'closed' for p in self.positions):
                self.history.append('blocked', symbol=opportunity.symbol, reason='同市场已有未结束持仓')
                return False
            if self.last_trade_time and (datetime.now() - self.last_trade_time).total_seconds() < self.trade_interval_min:
                self.history.append('blocked', symbol=opportunity.symbol, reason='尚未达到交易间隔')
                return False
            position = None
            first_receipt = None
            second_receipt = None
            quote_executor = None
            quote_future = None
            timing = {}
            current_stage = '提交前检查'
            previous_trade_time = self.last_trade_time
            previous_daily_trades = self.daily_trades
            try:
                with stage(self.history,'生成双腿计划',opportunity.symbol):
                    selected, v, l, step = self._prepared_plan(opportunity, _prepared) or self.plan_entry(opportunity)
                # Re-quote can legitimately flip the profitable direction.
                # Re-check the history gate on the direction that will
                # actually be submitted so a UI-approved route cannot be
                # replaced by an unfiltered opposite route.
                if not self._reversion_filter_allowed(
                        opportunity.symbol, selected.get('direction')):
                    self.history.append('blocked', symbol=opportunity.symbol,
                                        reason='刷新后的方向未通过历史回归筛选',
                                        direction=selected.get('direction'))
                    return False
                quantity = selected['quantity']
                initial_second_quote = dict(l)
                pause = self._route_pause_reason(opportunity.symbol, selected['direction'])
                if pause:
                    raise ValueError(pause)
                direction = selected['direction'] == 'sell_variational'
                prices = (float(v['bid']), float(l['ask'])) if direction else (float(v['ask']), float(l['bid']))
                if not self.gross_spread_allowed(prices[0], prices[1]):
                    self.history.append('blocked', symbol=opportunity.symbol,
                                        reason='刷新后的毛价差不在用户筛选范围内')
                    return False
                self.history.append('planned', symbol=opportunity.symbol,
                                    cap_usdc=selected.get('cap_usdc', self.entry_notional_cap()),
                                    price_movement_bps=selected.get('price_movement_bps', 0),
                                    edge_basis=selected.get('edge_basis', 'opening_edge'),
                                    target_context=selected.get('target_context'),
                                    target_projection=selected.get('target_projection'),
                                    **{k: selected[k] for k in ('direction', 'quantity', 'net_edge_bps',
                                       'estimated_entry_edge_usdc', 'variational_notional', 'lighter_notional')})
                # These are independent read-only account checks. Run them
                # together so the slower account does not add to the faster
                # account's latency; both still complete before any submit.
                capital_policy = self.entry_capital_policy()
                def account_check(name, trader, side):
                    with stage(self.history, name, opportunity.symbol):
                        if self.parallel_discovery:
                            quote = v if trader is self.variational_trader else l
                            return trader.precheck_order(opportunity.symbol, quantity, side,
                                allow_resize=True, quantity_step=step,
                                reference_price=max(float(quote['bid']), float(quote['ask'])),
                                cost_bps=max(0, float(self.slippage_tolerance)) +
                                         max(0, float(quote.get('fee_bps') or 0)),
                                **({'capital_policy': capital_policy} if capital_policy is not None else {}))
                        else:
                            if capital_policy is not None:
                                raise ValueError('当前平台不支持资金预算预检，首腿未提交')
                            trader.validate_order(opportunity.symbol, quantity)
                leverage_stage = '统一杠杆核验'
                def leverage_check():
                    with stage(self.history, leverage_stage, opportunity.symbol):
                        self._ensure_common_leverage(opportunity.symbol)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures=[]
                    account_checks = (
                        (self._stage_venue_label(self.variational_trader, 'V') + ' 账户预检',
                         self.variational_trader, 'sell' if direction else 'buy'),
                        (self._stage_venue_label(self.lighter_trader, 'L') + ' 账户预检',
                         self.lighter_trader, 'buy' if direction else 'sell'),
                    )
                    for name, trader, side in account_checks:
                        context=copy_context()
                        futures.append(pool.submit(context.run, account_check, name, trader, side))
                    checked_sizes = [entry_result(future) for future in futures]
                if self.parallel_discovery:
                    paired_quantity = min(Decimal(str(quantity)), *(Decimal(str(size)) for size in checked_sizes))
                    if paired_quantity < Decimal(str(quantity)):
                        for _, trader, side in account_checks:
                            trader.resize_entry_precheck(opportunity.symbol, paired_quantity, side)
                        self.history.append('entry_balance_resized', symbol=opportunity.symbol,
                            requested_quantity=quantity, quantity=float(paired_quantity),
                            **({'capital_policy':capital_policy} if capital_policy is not None else {}),
                            reason='按双边可用资金及预留预算统一缩量，随后重新核验同数量报价、最低金额及净收益')
                        quantity = float(paired_quantity)
                # Leverage is a separate signed account setting on Lighter;
                # it is deliberately applied only after both accounts pass
                # the no-existing-exposure checks and before any order submit.
                if self.leverage_mode != 'verify_only':
                    leverage_check()
                if self.parallel_discovery:
                    pre_final_quantity = quantity
                    with stage(self.history, '提交前最终报价', opportunity.symbol):
                        selected, v, l, step = self.plan_entry(
                            opportunity, fixed_quantity=quantity,
                            fixed_direction=selected['direction'], fixed_target=selected.get('target_context'),
                            allow_downsize=True)
                    final_quantity = selected['quantity']
                    if Decimal(str(final_quantity)) < Decimal(str(quantity)):
                        for _, trader, side in account_checks:
                            trader.resize_entry_precheck(opportunity.symbol, final_quantity, side)
                        quantity = final_quantity
                    prices = ((float(v['bid']), float(l['ask'])) if direction
                              else (float(v['ask']), float(l['bid'])))
                    if not self.gross_spread_allowed(prices[0], prices[1]):
                        raise ValueError('重新报价后净价差不满足条件')
                    self.history.append('entry_requoted', symbol=opportunity.symbol,
                                        quantity=quantity, direction=selected['direction'],
                                        requested_quantity=pre_final_quantity,
                                        book_utilization_percent=selected.get('book_utilization_percent'),
                                        paired_depth_quantity=selected.get('paired_depth_quantity'),
                                        depth_quantity_limit=selected.get('depth_quantity_limit'),
                                        net_edge_bps=selected['net_edge_bps'],
                                        edge_basis=selected.get('edge_basis', 'opening_edge'),
                                        target_context=selected.get('target_context'),
                                        target_projection=selected.get('target_projection'))
                    # Reuse the two existing quote rounds. A still-positive
                    # spread must not hide an unstable hedge-side price.
                    quote_side = 'ask' if direction else 'bid'
                    before, after = (float(initial_second_quote[quote_side]), float(l[quote_side]))
                    adverse_bps = max(0.0, (after / before - 1 if direction else 1 - after / before) * 10000)
                    if adverse_bps > selected['price_movement_bps'] + 1e-7:
                        raise ValueError('账户核验期间第二腿报价恶化超过价格变动预算，首腿未提交')
                # The final sized plan already clears the user's entry reserve.
                # Do not add a second hypothetical price/exit-credit stress
                # floor here. Keep both adapters' actual order-rule validation.
                current_stage = '双腿下单参数预检'
                validate_first = getattr(self.variational_trader, 'validate_quote_order', None)
                if callable(validate_first):
                    validate_first(opportunity.symbol, quantity, 'sell' if direction else 'buy', v,
                                   selected['sell_slippage_bps'] if direction else selected['buy_slippage_bps'])
                self.lighter_trader.validate_quote_order(opportunity.symbol, quantity, 'buy' if direction else 'sell', l,
                                                         selected['buy_slippage_bps'] if direction else selected['sell_slippage_bps'])
                current_stage = self._stage_venue_label(self.variational_trader, 'V') + '提交入场订单'
                position = Position(opportunity.symbol, 'short' if direction else 'long',
                                    'buy' if direction else 'sell', 0, 0, prices[0], prices[1], datetime.now())
                position.execution_id = self.history.current_execution_id()
                position.variational_venue = self._venue_name(self.variational_trader, 'variational')
                position.lighter_venue = self._venue_name(self.lighter_trader, 'lighter')
                position.entry_target = copy.deepcopy(selected.get('target_context'))
                position.entry_target_projection = copy.deepcopy(selected.get('target_projection'))
                self._grid_attach(position, selected.get('grid_context'), selected['direction'])
                self.positions.append(position)
                self.opportunity_observer.begin(position.id,
                    self._route_guard_key(position.symbol, selected['direction']), quantity)
                self.last_trade_time = datetime.now()
                self.daily_trades += 1  # Limit attempts, including rejected/rolled-back ones.
                self._save()  # Persist intent BEFORE a potentially ambiguous submission.
                self.last_execution_submitted = True
                hedge_started = time.monotonic()
                hedge_deadline = hedge_started + self.hedge_policy.max_delay_seconds
                self.history.append('entry_submit_intent', symbol=position.symbol,
                                    position_id=position.id,
                                    venue=self._venue_name(self.variational_trader, 'variational'),
                                    side=position.variational_side,
                                    quantity=quantity,
                                    reason='首腿提交即冻结平台组合；未知回执禁止切换路线')
                first_submit_started = time.monotonic()
                first = entry_call(self.variational_trader.place_order, position.symbol, position.variational_side,
                    quantity, price=prices[0], slippage_bps=selected['sell_slippage_bps'] if direction else selected['buy_slippage_bps'],
                    client_order_id=position.id + '-v', quote=v)
                first_submit_returned = time.monotonic()
                timing['first_submit_ms'] = round((first_submit_returned - first_submit_started) * 1000, 3)
                first_receipt = first
                filled = self._fill(first, quantity)
                first_venue = self._venue_name(self.variational_trader, 'variational')
                first_receipt_returned = time.monotonic()
                self._record_receipt('entry_receipt', position, first_venue, first,
                                     requested_size=quantity, timing=timing)
                position.variational_size = filled
                position.entry_size_variational = filled
                if filled:
                    position.entry_price_variational = float(first['average_price'])
                    try:
                        fee = float(first['fees_usdc'])
                        if not math.isfinite(fee):
                            raise ValueError('成交费用缺失')
                    except (ValueError, TypeError, KeyError):
                        position.accounting_pending = True
                        if first_venue not in position.pending_fee_venues:
                            position.pending_fee_venues.append(first_venue)
                        self._save()
                        self.history.append('accounting_pending', symbol=position.symbol,
                                            position_id=position.id, venue=first_venue,
                                            reason='首腿已成交但费用回执未知，未继续提交第二腿')
                        return self._unwind(position, category='accounting')
                    self.daily_pnl -= fee
                    position.entry_fees_usdc = (position.entry_fees_usdc or 0.0) + fee
                    position.realized_pnl_usdc = (position.realized_pnl_usdc or 0.0) - fee
                position.status = 'hedging'
                if not filled:
                    # A confirmed zero-fill rejection from the venue is safe:
                    # no exposure exists and the worker may try the next
                    # candidate.  Clear the submission marker so candidate
                    # fallback does not mistake this for an ambiguous send.
                    if (isinstance(first, dict) and first.get('confirmed') is True and
                            first.get('status') in ('rejected', 'canceled') and
                            float(first.get('filled_size', 0) or 0) == 0):
                        self.last_execution_submitted = False
                    # Form preparation can legitimately refresh the V RFQ.  If
                    # the adapter rejected before the submit click for that
                    # reason, re-plan both legs once against the new RFQ.  A
                    # terminal zero-fill pre-click rejection is safe to retry;
                    # an unknown or partial result remains fail-closed below.
                    retry_reason = str(first.get('error') or '')
                    if (_retry_requoted and first.get('confirmed') is True and
                            first.get('status') in ('rejected', 'canceled') and
                            'RFQ 已变化' in retry_reason and '未提交' in retry_reason):
                        position.status = 'closed'
                        self.last_trade_time = previous_trade_time
                        self.daily_trades = previous_daily_trades
                        self._save()
                        self.history.append('requote_retry', symbol=opportunity.symbol,
                                            reason='V 表单准备刷新 RFQ；未点击提交，重新核算双腿价差')
                        return self._execute_arbitrage(opportunity, _retry_requoted=False)
                    position.status = 'closed'
                    self._save()
                    return False
                # Partial first fills can violate the other venue's lot size: unwind.
                if Decimal(str(filled)) % step:
                    self._save()
                    return self._unwind(position, reason='首腿部分成交数量不符合第二腿数量步长', category='quantity')
                # Requote after first fill. A quote for a different size cannot be reused.
                current_stage = self._stage_venue_label(self.lighter_trader, 'L') + '提交对冲订单'
                # After a confirmed fill, the return floor determines how
                # much reserve can be consumed. Keep the user's hard order
                # slippage cap instead of reapplying the smaller entry budget.
                second_slippage = float(self.slippage_tolerance)
                first_price = float(first['average_price'])
                second_quote = None
                second_price = None
                bound = None
                # Start the independent post-fill quote reads before the
                # durable position snapshot.  The snapshot remains mandatory
                # for crash recovery, while the network wait overlaps with
                # local JSON/fsync work instead of extending the hedge window.
                first_receipt_to_quote_started = time.monotonic()
                timing['first_receipt_processing_ms'] = round(
                    (first_receipt_to_quote_started - first_receipt_returned) * 1000, 3)
                quote_started = time.monotonic()
                timing['first_receipt_to_second_quote_ms'] = round(
                    (quote_started - first_receipt_returned) * 1000, 3)
                quote_executor = ThreadPoolExecutor(max_workers=1)
                quote_future = quote_executor.submit(
                    copy_context().run, self._quotes, position.symbol, filled)
                # Keep the first-fill receipt durable, but overlap its local
                # snapshot with the already-started independent quote reads.
                self._save()
                for hedge_attempt in range(self.second_leg_retries + 1):
                    if time.monotonic() >= hedge_deadline:
                        self.history.append('hedge_leg_skipped', symbol=position.symbol,
                            position_id=position.id, reason_code='timeout', reason='补齐对冲提交窗口超时，立即回补首腿',
                            first_submit_ms=timing.get('first_submit_ms'),
                            first_receipt_processing_ms=timing.get('first_receipt_processing_ms'),
                            first_receipt_to_second_quote_ms=timing.get('first_receipt_to_second_quote_ms'))
                        return self._unwind(position, category='timeout')
                    if hedge_attempt == 0 and quote_future is not None:
                        first_quote, second_quote = entry_result(quote_future)
                        quote_returned = time.monotonic()
                        timing['second_quote_ms'] = round((quote_returned - quote_started) * 1000, 3)
                    else:
                        retry_quote_started = time.monotonic()
                        first_quote, second_quote = self._quotes(position.symbol, filled)
                        quote_returned = time.monotonic()
                        timing['second_quote_ms'] = round((quote_returned - retry_quote_started) * 1000, 3)
                    second_price = float(second_quote['ask'] if direction else second_quote['bid'])
                    window_ms = (time.monotonic() - hedge_started) * 1000
                    entry_cap = float(selected.get('cap_usdc', self.entry_notional_cap()))
                    hedge_minimum = max(0.0, float(self.spread_threshold_min))
                    grid_context = selected.get('grid_context')
                    if grid_context and grid_context['layer'] > 0:
                        hedge_minimum = max(hedge_minimum, grid_context.get('hedge_minimum_bps', grid_context['minimum_bps']))
                    if position.entry_target:
                        projection = self._completion_target_projection(selected, first_quote, second_quote, first_price)
                        position.entry_target_projection = projection
                        minimum_usdc = self._target_profit_floor_usdc(filled, projection)
                        bound, price_budget = completion_target_limit(first_price, first['fees_usdc'], filled,
                            prices[1], second_quote['fee_bps'], second_slippage,
                            hedge_minimum, entry_cap, direction, projection, minimum_usdc=minimum_usdc)
                        net, net_bps = completion_return(first_price, first['fees_usdc'], filled,
                            bound, second_quote['fee_bps'], direction, projection)
                        _, observed_net_bps = completion_return(first_price, first['fees_usdc'], filled,
                            second_price, second_quote['fee_bps'], direction, projection)
                    else:
                        bound, price_budget = completion_limit(first_price, first['fees_usdc'], filled,
                            prices[1], second_quote['fee_bps'], second_slippage,
                            hedge_minimum, entry_cap, direction)
                        net, net_bps = completion_edge(first_price, first['fees_usdc'], filled,
                                                      bound, second_quote['fee_bps'], direction)
                        _, observed_net_bps = completion_edge(first_price, first['fees_usdc'], filled,
                                                             second_price, second_quote['fee_bps'], direction)
                    projection_decay_bps = (max(0.0,
                        (selected['target_projection']['exit_credit_per_unit'] -
                         projection['exit_credit_per_unit']) / max(first_price, second_price) * 10000)
                        if position.entry_target else 0.0)
                    self.execution_window_risk.observe(
                        self._route_guard_key(position.symbol, selected['direction']),
                        prices[0], first_price, prices[1], second_price, direction, window_ms,
                        projection_decay_bps=projection_decay_bps, attempt_id=position.id)
                    within_bound = second_price <= bound if direction else second_price >= bound
                    timely = time.monotonic() < hedge_deadline
                    profitable = (net > 0 and net_bps + 1e-7 >= hedge_minimum and
                                  (not position.entry_target or net + 1e-10 >= minimum_usdc))
                    within_cap = filled * max(first_price, second_price) <= entry_cap + 1e-10
                    within_price = second_price <= price_budget if direction else second_price >= price_budget
                    failures = hedge_failures(timely=timely, within_cap=within_cap, within_price=within_price,
                        observed_net_bps=observed_net_bps, minimum_bps=hedge_minimum)
                    if not profitable and 'edge' not in failures:
                        failures.append('edge')
                    try:
                        unwind_pnl = unwind_estimate(first_price, first['fees_usdc'], filled,
                            first_quote, direction, selected.get('price_movement_bps', self.hedge_policy.price_movement_bps))
                    except (KeyError, TypeError, ValueError, ArithmeticError):
                        unwind_pnl = None
                    decision_finished = time.monotonic()
                    timing['second_decision_ms'] = round((decision_finished - quote_returned) * 1000, 3)
                    timing['first_receipt_to_second_send_ms'] = None
                    self.history.append('hedge_decision', symbol=position.symbol, position_id=position.id,
                        observed_price=second_price, protected_bound=bound, price_budget_bound=price_budget,
                        hedge_edge_usdc=net, net_edge_bps=net_bps, hedge_min_net_bps=hedge_minimum,
                        observed_net_edge_bps=observed_net_bps,
                        execution_window_ms=window_ms, second_reference_price=prices[1],
                        reason_code=failures[0] if failures else None, constraint_failures=failures,
                        entry_reserve_bps=self.execution_safety_margin_bps, base_min_edge_bps=self.spread_threshold_min,
                        edge_basis='target_return' if position.entry_target else 'opening_edge',
                        target_context=position.entry_target, target_projection=position.entry_target_projection,
                        unwind_pnl_estimate_usdc=unwind_pnl, attempt=hedge_attempt + 1,
                        first_submit_ms=timing.get('first_submit_ms'),
                        first_receipt_processing_ms=timing.get('first_receipt_processing_ms'),
                        first_receipt_to_second_quote_ms=timing.get('first_receipt_to_second_quote_ms'),
                        second_quote_ms=timing.get('second_quote_ms'),
                        second_decision_ms=timing.get('second_decision_ms'),
                        decision=('hedge' if within_bound and timely and profitable and within_cap
                                  else 'recheck' if timely and hedge_attempt < self.second_leg_retries else 'unwind'),
                        reason='对冲价差净值与立即回补损益分别估算；前者不等于最终已实现利润')
                    if within_bound and timely and profitable and within_cap:
                        break
                    if hedge_attempt < self.second_leg_retries and timely:
                        self.history.append(
                            'hedge_leg_retry', symbol=position.symbol,
                            position_id=position.id,
                            venue=self._venue_name(self.lighter_trader, 'lighter'),
                            reason='第二腿保护条件未满足，立即重新报价一次',
                            attempt=hedge_attempt + 1,
                            observed_price=second_price, protected_bound=bound,
                            execution_total_ms=self.history.current_elapsed_ms())
                        continue
                    category = failures[0] if failures else 'edge'
                    reason = '第二腿' + '、'.join(REASON_LABELS[c] for c in (failures or ['edge']))
                    self.history.append(
                        'hedge_leg_skipped', symbol=position.symbol,
                        position_id=position.id,
                        venue=self._venue_name(self.lighter_trader, 'lighter'),
                        reason=reason + '，未提交；立即回补第一腿',
                        reason_code=category, constraint_failures=failures,
                        observed_price=second_price, protected_bound=bound,
                        execution_total_ms=self.history.current_elapsed_ms(),
                        first_submit_ms=timing.get('first_submit_ms'),
                        first_receipt_processing_ms=timing.get('first_receipt_processing_ms'),
                        first_receipt_to_second_quote_ms=timing.get('first_receipt_to_second_quote_ms'),
                        second_quote_ms=timing.get('second_quote_ms'),
                        second_decision_ms=timing.get('second_decision_ms'),
                        first_receipt_to_second_send_ms=timing.get('first_receipt_to_second_send_ms'))
                    return self._unwind(position, category=category)
                second_quote = dict(second_quote, submit_deadline_monotonic=hedge_deadline)
                second_submit_started = time.monotonic()
                timing['first_receipt_to_second_send_ms'] = round(
                    (second_submit_started - first_receipt_returned) * 1000, 3)
                second = entry_call(self.lighter_trader.place_order, position.symbol, position.lighter_side,
                    filled, price=second_price, slippage_bps=max(0, min(second_slippage,
                        ((bound / second_price - 1) if direction else (1 - bound / second_price)) * 10000)),
                    client_order_id=position.id + '-l', quote=second_quote)
                second_submit_returned = time.monotonic()
                timing['second_submit_ms'] = round((second_submit_returned - second_submit_started) * 1000, 3)
                second_receipt = second
                position.lighter_size = self._fill(second, filled)
                position.entry_size_lighter = position.lighter_size
                second_venue = self._venue_name(self.lighter_trader, 'lighter')
                self._record_receipt('entry_receipt', position, second_venue, second,
                                     requested_size=filled, timing=timing)
                if position.lighter_size:
                    position.entry_price_lighter = float(second['average_price'])
                    try:
                        fee = float(second['fees_usdc'])
                        if not math.isfinite(fee):
                            raise ValueError('成交费用缺失')
                    except (ValueError, TypeError, KeyError):
                        position.accounting_pending = True
                        self._save()
                        self.history.append('accounting_pending', symbol=position.symbol,
                                            position_id=position.id, venue=second_venue,
                                            reason='第二腿已成交但费用回执未知，未确认双腿账务')
                        return self._unwind(position, category='accounting')
                    self.daily_pnl -= fee
                    position.entry_fees_usdc = (position.entry_fees_usdc or 0.0) + fee
                    position.realized_pnl_usdc = (position.realized_pnl_usdc or 0.0) - fee
                self._save()
                if abs(position.variational_size - position.lighter_size) > 1e-12:
                    receipt_reason = safe_reason(ValueError(second['error'])) if second.get('error') else ''
                    return self._unwind(position, reason='第二腿回执 ' + str(second.get('status') or '未知')
                                        + '，两腿实际成交数量不一致' + ('；' + receipt_reason if receipt_reason else ''),
                                        category=('partial_fill' if position.lighter_size > 0 else
                                                  'zero_fill' if second.get('status') in ('canceled', 'filled', 'partial') else 'rejected'))
                position.status = 'open'
                # Order terminal pushes can lead the REST account view. Only
                # this process's newly confirmed paired fills get a bounded
                # settlement window; resumed/unknown orders never inherit it.
                self._entry_position_settling[position.id] = time.monotonic() + 5
                position.actual_entry_spread_bps = self._actual_entry_spread_bps(position)
                if position.entry_target:
                    q = position.entry_size_variational
                    gross = (position.entry_price_variational - position.entry_price_lighter) * (1 if direction else -1)
                    net_unit = gross + position.entry_target_projection['exit_credit_per_unit'] - position.entry_fees_usdc / q
                    position.target_entry_net_bps = net_unit / max(position.entry_price_variational, position.entry_price_lighter) * 10000
                self._grid_filled(position)
                position.execution_elapsed_ms = self.history.current_elapsed_ms()
                self._reset_route_rollbacks(position.symbol, selected['direction'])
                self.execution_window_risk.outcome(self._route_guard_key(position.symbol, selected['direction']), 'paired',
                    filled * max(position.entry_price_variational, position.entry_price_lighter),
                    ceiling=selected.get('size_recovery_cap_usdc'))
                self.opportunity_observer.outcome(position.id, 'paired',
                    first_fill_price=position.entry_price_variational, second_fill_price=position.entry_price_lighter,
                    filled_quantity=filled, first_submit_ms=timing.get('first_submit_ms'),
                    first_receipt_to_second_send_ms=timing.get('first_receipt_to_second_send_ms'))
                self._save()
                self.history.append('hedged', symbol=position.symbol, position_id=position.id, quantity=filled,
                                    actual_spread_bps=position.actual_entry_spread_bps,
                                    target_context=position.entry_target,
                                    target_entry_net_bps=position.target_entry_net_bps,
                                    execution_total_ms=position.execution_elapsed_ms,
                                    first_submit_ms=timing.get('first_submit_ms'),
                                    first_receipt_processing_ms=timing.get('first_receipt_processing_ms'),
                                    first_receipt_to_second_quote_ms=timing.get('first_receipt_to_second_quote_ms'),
                                    second_quote_ms=timing.get('second_quote_ms'),
                                    second_decision_ms=timing.get('second_decision_ms'),
                                    second_submit_ms=timing.get('second_submit_ms'),
                                    first_receipt_to_second_send_ms=timing.get('first_receipt_to_second_send_ms'))
                return True
            except Exception as exc:
                raw_reason = str(exc)
                reason = safe_reason(exc)
                # Keep the public diagnostic safe while retaining the concrete
                # stage.  A bare ``ValueError`` used to leave the user unable
                # to tell whether the failure was preflight, first-leg submit,
                # requote, or hedge-leg submit.
                self.history.append('blocked', symbol=opportunity.symbol,
                                     reason=reason, stage=current_stage,
                                     execution_total_ms=self.history.current_elapsed_ms())
                # A temporary account-capacity failure should not make the
                # worker hammer every symbol once per second.  Back off new
                # entry discovery, while the worker still keeps any existing
                # hedge under its normal close monitor.
                lowered = raw_reason.lower()
                self.record_entry_scan_failure(exc)
                if any(token in lowered for token in ('余额不足', '可用余额', '保证金不足', 'margin', 'insufficient balance', 'collateral')):
                    if len(self._route_trader_pairs) > 1:
                        self._entry_balance_backoff[self._active_route_index] = time.monotonic() + 30
                    else:
                        self.entry_scan_backoff_until = datetime.now() + timedelta(seconds=30)
                        self.entry_scan_backoff_reason = '账户余额或保证金不足，暂停新开仓扫描 30 秒；已有仓位仍继续监控'
                if position is not None:
                    # A terminal, confirmed zero-fill rejection is safe to close.
                    # Only unknown/partial/filled outcomes require reconciliation.
                    receipts = [first_receipt, second_receipt]
                    deterministic_reject = any(
                        isinstance(receipt, dict) and receipt.get('confirmed') is True and
                        receipt.get('status') in ('rejected', 'canceled') and
                        float(receipt.get('filled_size', 0) or 0) == 0
                        for receipt in receipts)
                    if deterministic_reject and all(
                        receipt is None or (receipt.get('confirmed') is True and
                                            receipt.get('status') in ('rejected', 'canceled') and
                                            float(receipt.get('filled_size', 0) or 0) == 0)
                        for receipt in receipts):
                        position.status = 'closed'
                        position.variational_size = position.lighter_size = 0
                        self._save()
                        self.history.append('entry_rejected', symbol=position.symbol,
                                            position_id=position.id, reason='平台确认拒单，0 成交；未产生敞口',
                                            execution_total_ms=self.history.current_elapsed_ms())
                    else:
                        # Never retry or blindly unwind an order whose fill is unknown.
                        position.status = 'reconcile_required'
                        self.halted_reason = '订单状态需核对: ' + type(exc).__name__
                        self._save()
                return False

            finally:
                if position is not None:
                    self.opportunity_observer.outcome(position.id, position.status,
                        reason_code=position.rollback_category, stage=current_stage)
                if quote_executor is not None:
                    # The first quote batch may still be completing after an
                    # early rejection/unwind.  Never wait for it on the hot
                    # failure path; the worker can safely discard its result.
                    if quote_future is not None and not quote_future.done():
                        quote_future.cancel()
                    try:
                        quote_executor.shutdown(wait=False, cancel_futures=True)
                    except TypeError:  # Python versions without cancel_futures
                        quote_executor.shutdown(wait=False)
                if self.parallel_discovery:
                    for trader in (self.variational_trader, self.lighter_trader):
                        trader.release_entry_precheck()

    def _unwind(self, position, reason=None, category=None):
        position.rollback_category = category or position.rollback_category or 'unknown'
        if reason:
            self.history.append('entry_rollback', symbol=position.symbol, position_id=position.id, reason=reason,
                                reason_code=position.rollback_category)
        self._record_route_rollback(position)
        self.close_position(position, reason='第二腿未成对·回补第一腿')
        return False

    def _close_client_order_id(self, position, name):
        """Build a short, unique reduce-only client id for every venue.

        Arcus rejects client ids longer than 36 characters.  Position ids are
        already 32 hex characters, so appending a verbose venue suffix made
        every Arcus close request fail before it reached the exchange.  Keep
        a compact position prefix for auditability and a random suffix for
        retries; the durable order journal still stores the full intent.
        """
        prefix = ''.join(ch for ch in str(position.id) if ch.isalnum())[:16]
        venue = str(self._venue_name(
            self.variational_trader if name == 'variational' else self.lighter_trader,
            name)).replace('_', '')[:3] or name[:1]
        return f'{prefix}-c-{venue}-{uuid.uuid4().hex[:8]}'[:36]

    @staticmethod
    def _snapshot_side(value):
        value = str(value or '').lower()
        if value in ('long', 'buy'):
            return 'long'
        if value in ('short', 'sell'):
            return 'short'
        return None

    def reconcile_open_positions(self, min_interval_seconds=5):
        """Compare durable hedge legs with both authenticated account states.

        A manual close is an external state change. When both legs are flat,
        the worker pauses and lets the existing read-only reconciliation path
        settle the local record. When exactly one leg remains, the worker
        enters the single-leg recovery gate; that gate requires fresh account
        state, a known closed-leg receipt, positive whole-trade PnL and a
        reduce-only close before it can submit the remaining-leg exit.
        """
        active = [position for position in self.positions if position.status == 'open']
        if not active:
            return 'none'
        now = time.monotonic()
        monitor = self.position_monitor
        if (not monitor and self._last_position_reconcile_result is not None and
                now - self._last_position_reconcile_at < max(0.0, float(min_interval_seconds))):
            return 'unavailable' if self._last_position_reconcile_result == 'unavailable' else 'throttled'
        self._last_position_reconcile_at = now
        groups = {}
        for item in active:
            groups.setdefault(item.symbol, []).append(item)
        for group in groups.values():
            position = group[0]
            try:
                readers = self.position_traders(position)
                if any(self.position_traders(p) != readers for p in group):
                    raise ValueError('同品种持仓平台组合不一致，必须先核对原仓位')
            except ValueError as exc:
                self.halted_reason = str(exc)
                self._save()
                return 'halted'
            if any(not callable(getattr(trader, 'position_snapshot', None)) for trader in readers):
                return 'unsupported'
            if any(self._snapshot_side(p.variational_side) != self._snapshot_side(position.variational_side) or
                   self._snapshot_side(p.lighter_side) != self._snapshot_side(position.lighter_side)
                   for p in group):
                self.halted_reason = '同市场本地层方向不一致，禁止继续下单'
                self._save()
                return 'halted'
            try:
                snapshots = monitor.accounts(position.symbol) if monitor else (
                    readers[0].position_snapshot(position.symbol),
                    readers[1].position_snapshot(position.symbol),
                )
            except Exception as exc:
                # Do not place a reduce-only order on a stale or failed
                # snapshot.  The next monitor tick can retry the read.
                self._last_position_reconcile_result = 'unavailable'
                if not getattr(self, '_position_reconcile_error_logged', False):
                    self._position_reconcile_error_logged = True
                    self.history.append_diagnostic('position_reconcile_failed', symbol=position.symbol,
                                                   position_id=position.id, reason=type(exc).__name__)
                return 'unavailable'
            self._position_reconcile_error_logged = False
            expected = (
                (sum(float(p.variational_size or 0) for p in group), self._snapshot_side(position.variational_side)),
                (sum(float(p.lighter_size or 0) for p in group), self._snapshot_side(position.lighter_side)),
            )
            mismatches = []
            actual_quantities = []
            possibly_lagging = True
            for venue, snapshot, (expected_qty, expected_side) in zip(
                    ('variational', 'lighter'), snapshots, expected):
                try:
                    actual_qty = float(snapshot.get('qty', 0) or 0)
                    if not math.isfinite(actual_qty) or actual_qty < 0:
                        actual_qty = None
                except (AttributeError, TypeError, ValueError):
                    actual_qty = None
                actual_quantities.append(actual_qty)
                actual_side = self._snapshot_side(snapshot.get('side')) if isinstance(snapshot, dict) else None
                tolerance = max(1e-8, abs(expected_qty) * 1e-6)
                if (actual_qty is None or actual_qty > expected_qty + tolerance or
                        actual_qty > tolerance and actual_side != expected_side):
                    possibly_lagging = False
                if actual_qty is None or actual_qty > tolerance and (
                        expected_qty <= tolerance or abs(actual_qty - expected_qty) > tolerance or
                        actual_side != expected_side):
                    mismatches.append(venue)
                elif actual_qty <= tolerance and expected_qty > tolerance:
                    mismatches.append(venue)
            if not mismatches:
                for peer in group:
                    self._entry_position_settling.pop(peer.id, None)
                continue
            if possibly_lagging and any(now < self._entry_position_settling.get(peer.id, 0) for peer in group):
                # Pause all account-dependent actions while asking for a new
                # read. Never convert this lagging snapshot into a fill or a
                # manual close, and never place another order from it.
                if monitor:
                    monitor.refresh_accounts(position.symbol)
                self._last_position_reconcile_result = 'unavailable'
                return 'unavailable'
            both_flat = all(quantity is not None and quantity <= 1e-8
                            for quantity in actual_quantities)
            position.status = 'reconcile_required'
            if both_flat:
                position.close_reason = '检测到账户已手动平仓；等待只读核对确认'
                self.halted_reason = '检测到账户已手动平仓；收益与费用待核对，禁止继续下单'
                event = 'external_close_detected'
                reason = '两平台账户均已空仓，但程序未收到平仓回执'
            else:
                position.close_reason = '检测到账户持仓与程序记录不一致；等待只读核对'
                self.halted_reason = '检测到账户持仓与程序记录不一致；必须先核对两边账户'
                event = 'external_position_mismatch'
                reason = '至少一条持仓腿与程序记录不一致'
            for peer in group:
                peer.status = 'reconcile_required'
                peer.close_reason = position.close_reason
            self._save()
            self.history.append(event, symbol=position.symbol, position_id=position.id,
                                reason=reason, mismatched_venues=mismatches,
                                expected_positions=[dict(qty=qty, side=side) for qty, side in expected],
                                actual_positions=[dict(qty=qty,
                                    side=s.get('side') if isinstance(s, dict) else None,
                                    fetched_at=s.get('fetched_at') if isinstance(s, dict) else None)
                                    for qty, s in zip(actual_quantities, snapshots)])
            return 'halted'
        self._last_position_reconcile_result = 'ok'
        return 'ok'

    def _single_leg_snapshot(self, position):
        """Return the one real remaining leg when the other account is flat."""
        try:
            readers = self.position_traders(position)
        except ValueError:
            return None
        if any(not callable(getattr(trader, 'position_snapshot', None)) for trader in readers):
            return None
        try:
            snapshots = [trader.position_snapshot(position.symbol) for trader in readers]
        except Exception:
            return None
        expected = (
            (float(position.variational_size or 0), self._snapshot_side(position.variational_side)),
            (float(position.lighter_size or 0), self._snapshot_side(position.lighter_side)),
        )
        actual = []
        for snapshot in snapshots:
            try:
                qty = float(snapshot.get('qty', 0) or 0)
            except (AttributeError, TypeError, ValueError):
                return None
            actual.append((qty, self._snapshot_side(snapshot.get('side')) if isinstance(snapshot, dict) else None))
        live = [(index, qty, side) for index, (qty, side) in enumerate(actual) if qty > 1e-8]
        if len(live) != 1:
            return None
        index, qty, side = live[0]
        expected_qty, expected_side = expected[index]
        tolerance = max(1e-8, abs(expected_qty) * 1e-6)
        if expected_qty <= tolerance or side != expected_side or qty > expected_qty + tolerance:
            return None
        name = 'variational' if index == 0 else 'lighter'
        trader = readers[index]
        # A resumed program exit must match its durable receipts. Silently
        # adopting a smaller account quantity would hide a manual fill and
        # allow a retry with incomplete accounting evidence.
        if not (position.close_started_time and position.close_order_results):
            setattr(position, name + '_size', qty)
        return name, trader, expected[index][1], qty

    def _single_leg_recovery_estimate(self, position, name, trader, side, size):
        """Estimate whole-trade PnL from a known closed leg and live exit quote."""
        # Unknown trading fees make the profitability test unsafe. Funding is
        # allowed to remain pending because the recovery gate can still use a
        # conservative trading-PnL estimate and keep the account halted until
        # the later funding reconciliation completes.
        if position.pending_fee_venues or position.realized_pnl_usdc is None:
            return None
        closed_name = 'lighter' if name == 'variational' else 'variational'
        if getattr(position, 'close_price_' + closed_name) is None:
            return None
        try:
            quote = trader.get_executable_quote(position.symbol, size)
            close_side = 'sell' if side in ('buy', 'long') else 'buy'
            close_price = float(quote['bid'] if close_side == 'sell' else quote['ask'])
            fee_bps = float(quote.get('fee_bps', 0) or 0)
            entry_price = float(getattr(position, 'entry_price_' + name))
            entry_size = float(getattr(position, 'entry_size_' + name) or size)
            sign = 1 if side in ('long', 'buy') else -1
            remaining_pnl = sign * (close_price - entry_price) * size
            remaining_pnl -= close_price * size * fee_bps / 10000.0
            combined = float(position.realized_pnl_usdc) + remaining_pnl
            closed_entry = float(getattr(position, 'entry_price_' + closed_name))
            closed_size = float(getattr(position, 'entry_size_' + closed_name) or 0)
            entry_notional = abs(closed_entry * closed_size) + abs(entry_price * entry_size)
            pnl_bps = combined / max(entry_notional, 1e-12) * 10000.0
            minimum = float(self.close_min_profit_usdc or 0) + float(self.close_cost_reserve_usdc or 0)
            return {'allowed': combined > 0 and combined >= minimum and pnl_bps >= float(self.close_min_profit_bps or 0),
                    'combined_pnl_usdc': combined, 'pnl_bps': pnl_bps,
                    'quote': quote, 'close_side': close_side, 'close_price': close_price}
        except (AttributeError, KeyError, TypeError, ValueError, ArithmeticError):
            return None

    def recover_single_leg_positions(self):
        """Resume confirmed exits; discretionary recovery retains its profit gate."""
        if not self.single_leg_recovery_enabled:
            return 'blocked'
        candidates = [p for p in self.positions if p.status in ('open', 'reconcile_required')]
        if not candidates:
            return 'none'
        for position in candidates:
            snapshot = self._single_leg_snapshot(position)
            if snapshot is None:
                continue
            name, trader, side, size = snapshot
            previous = position.close_order_results.get(name, {})
            if position.close_started_time and previous:
                if previous.get('confirmed') is not True:
                    self.halted_reason = '上一笔平仓回执未确认，需只读核对后处理'
                    self._save()
                    return 'blocked'
                position.status = 'open'
                self.halted_reason = None
                recovered = self.close_position(position, reason=position.close_reason or '平仓残腿重试')
                if recovered or not position.variational_size and not position.lighter_size:
                    return 'recovered'
                return 'blocked'
            estimate = self._single_leg_recovery_estimate(position, name, trader, side, size)
            if estimate is None:
                self.halted_reason = '单腿修复缺少已平仓成交价或账务证据，需人工核对'
                self.history.append('single_leg_recovery_blocked', symbol=position.symbol,
                                    position_id=position.id, reason='缺少历史成交价或费用证据')
                self._save()
                return 'blocked'
            if not estimate['allowed']:
                self.halted_reason = '单腿修复等待组合净盈利条件'
                self.history.append('single_leg_recovery_waiting', symbol=position.symbol,
                                    position_id=position.id, remaining_venue=self._venue_name(trader, name),
                                    combined_pnl_usdc=estimate['combined_pnl_usdc'],
                                    pnl_bps=estimate['pnl_bps'])
                self._save()
                return 'waiting'
            attempts = self._single_leg_recovery_attempts.get(position.id, 0)
            if attempts >= self.single_leg_recovery_max_attempts:
                self.halted_reason = '单腿修复次数已达上限，需人工核对'
                self._save()
                return 'blocked'
            self._single_leg_recovery_attempts[position.id] = attempts + 1
            previous_halt = self.halted_reason
            position.status = 'open'
            self.halted_reason = None
            self._save()
            recovered = self.close_position(position, reason='单腿修复·组合达到盈利')
            if recovered:
                self.history.append('single_leg_recovered', symbol=position.symbol,
                                    position_id=position.id, venue=self._venue_name(trader, name),
                                    combined_pnl_usdc=estimate['combined_pnl_usdc'],
                                    pnl_bps=estimate['pnl_bps'])
                return 'recovered'
            if (not position.variational_size and not position.lighter_size and
                    not position.pending_fee_venues):
                self.history.append('single_leg_recovered', symbol=position.symbol,
                                    position_id=position.id, venue=self._venue_name(trader, name),
                                    combined_pnl_usdc=estimate['combined_pnl_usdc'],
                                    pnl_bps=estimate['pnl_bps'], accounting_pending=True)
                return 'recovered'
            if not self.halted_reason:
                self.halted_reason = previous_halt or '单腿修复未确认完成，需核对账户'
            self._save()
            return 'blocked'
        return 'blocked'

    def close_position(self, position, reason=None):
        with self.lock:
            if self.halted_reason or position.status in ('closed', 'reconcile_required'):
                return False
            try:
                self.position_traders(position)
            except ValueError as exc:
                self.halted_reason = '平仓受阻：' + safe_reason(exc)
                position.status = 'reconcile_required'
                self._save()
                return False
            with self.position_route(position):
                return self._close_position(position, reason)

    def _close_position(self, position, reason=None):
        with self.lock, CloseRetryReads() as retry_reads:
            if self.halted_reason or position.status in ('closed', 'reconcile_required'):
                return False
            try:
                legs = self._close_legs(position)
            except (ValueError, TypeError) as exc:
                self.halted_reason = '平仓受阻：' + safe_reason(exc)
                position.status = 'reconcile_required'
                self._save()
                return False
            exit_plan = None
            resuming_exit = bool(position.close_started_time and self._exit_has_fills(position))
            if position.grid_cycle_id and reason == '网格逐层净收益退出' and not resuming_exit:
                try:
                    exit_plan = self._grid_exit_estimate(position)
                except Exception:
                    return False
                if not self._grid_profit_ok(exit_plan):
                    return False
            if not position.grid_cycle_id and reason in NORMAL_EXIT_REASONS and not resuming_exit:
                try:
                    exit_plan = self._single_exit_estimate(position)
                except Exception as exc:
                    self._defer_profit_exit(position, '发送前净盈利核验不可用：' + safe_reason(exc))
                    return False
                if not self._single_profit_ok(exit_plan):
                    self._defer_profit_exit(position, '发送前净盈利未达标', exit_plan)
                    return False
                self._record_close_diagnostic(position, reason, exit_plan, allowed=True)
            elif reason in ('最长持仓保护', '单笔亏损保护'):
                self._record_close_diagnostic(position, reason, None, allowed=True)
            close_started = position.close_started_time or datetime.now()
            position.close_started_time = close_started
            if reason:
                position.close_reason = reason
            position.status = 'closing'
            self._save()
            if legs and any(position.close_attempts.get(name, 0) for name, _, _, _ in legs):
                try:
                    self._verify_close_retry(position, legs, retry_reads, prepare_quotes=exit_plan is None)
                except Exception as exc:
                    self.halted_reason = '平仓重试受阻：' + safe_reason(exc)
                    position.status = 'reconcile_required'
                    self._save()
                    return False
            while legs:
                failures, batch_filled = [], 0
                try:
                    for job, receipt, error in self._parallel_close_batch(position, legs, exit_plan, retry_reads):
                        name, trader, side, size, client_id, attempt = job
                        try:
                            if error:
                                raise error
                            batch_filled += self._apply_close_receipt(position, name, side, size, receipt)
                        except Exception as exc:
                            # Every peer result is still collected and persisted.
                            # A timeout/malformed receipt never becomes zero-fill.
                            failures.append(type(exc).__name__)
                            position.close_order_results[name].update(
                                confirmed=False, status='unknown', error_class=type(exc).__name__)
                            self.history.append('close_receipt_unknown', symbol=position.symbol,
                                position_id=position.id, venue=self._venue_name(trader, name),
                                attempt=attempt, reason=safe_reason(exc))
                            self._save()
                except Exception as exc:
                    if exit_plan and not position.close_attempts:
                        position.status = 'open'
                        position.close_started_time = None
                        position.close_reason = None
                        self.history.append('grid_exit_deferred' if position.grid_cycle_id else 'profit_exit_deferred', symbol=position.symbol,
                            position_id=position.id, reason='双腿发送前报价已失效；重新等待净盈利退出条件')
                        self._save()
                        return False
                    failures.append(type(exc).__name__)
                if failures:
                    self.halted_reason = '平仓回执需核对：' + '、'.join(sorted(set(failures)))
                    position.status = 'reconcile_required'
                    self._save()
                    return False
                legs = self._close_legs(position)
                if not legs:
                    break
                if exit_plan and not batch_filled:
                    # With no fills, re-evaluate whole-trade profitability.
                    # A peer fill instead commits us to reducing the residual.
                    position.status = 'open'
                    position.close_started_time = None
                    position.close_reason = None
                    position.close_attempts = {}
                    self.history.append('grid_exit_deferred' if position.grid_cycle_id else 'profit_exit_deferred', symbol=position.symbol,
                        position_id=position.id, reason='双腿均确认零成交；重新等待整组净收益条件')
                    self._save()
                    return False
                try:
                    self._verify_close_retry(position, legs, retry_reads)
                except Exception as exc:
                    self.halted_reason = '平仓重试受阻：' + safe_reason(exc)
                    position.status = 'reconcile_required'
                    self.history.append('close_retry_blocked', symbol=position.symbol,
                        position_id=position.id, reason=self.halted_reason)
                    self._save()
                    return False
                self.history.append('close_residual_retry', symbol=position.symbol,
                    position_id=position.id, reason='平仓终态与剩余持仓已核对；按现有滑点上限继续只减仓，无需等待组合盈利')
                exit_plan = None
            fills_finished = datetime.now()
            position.close_execution_ms = round((fills_finished - close_started).total_seconds() * 1000)
            defer_funding = self.position_monitor is not None
            accounting_pending = bool(position.pending_fee_venues) if defer_funding else position.accounting_pending
            # Funding can settle after the close receipt.  Query each leg's
            # read-only settlement endpoint before deciding whether the row
            # needs manual accounting review.
            if position.pending_funding_venues and not defer_funding:
                now = datetime.now()
                for legacy_name in list(position.pending_funding_venues):
                    trader = (self.variational_trader if legacy_name == 'variational'
                              else self.lighter_trader if legacy_name == 'lighter' else None)
                    if trader is None or not hasattr(trader, 'reconcile_funding'):
                        continue
                    try:
                        settlement = trader.reconcile_funding(position.symbol, position.open_time, now)
                    except Exception:
                        settlement = None
                    if not settlement or settlement.get('complete') is not True:
                        continue
                    settlement = self._allocate_layer_funding(settlement, position, legacy_name)
                    if settlement is None:
                        continue
                    position.funding_settlements[legacy_name] = settlement
                    amount = float(settlement.get('funding_usdc') or 0)
                    self.daily_pnl += amount
                    position.realized_pnl_usdc = float(position.realized_pnl_usdc or 0) + amount
                    position.pending_funding_venues.remove(legacy_name)
                    self.history.append('funding_reconciled', symbol=position.symbol,
                        position_id=position.id, venue=settlement.get('venue'),
                        funding_usdc=amount)
                # A deferred funding lookup is an attention state, but it is
                # not a fee failure.  Keep the separate list authoritative.
                position.accounting_pending = bool(position.pending_fee_venues) or (
                    bool(position.pending_funding_venues) and
                    self.config.get('funding_accounting_mode') != 'defer_settlement')
                accounting_pending = position.accounting_pending
            if accounting_pending:
                self.halted_reason = '两腿已按回执减仓，但费用或资金费未知；核对前禁止新开仓'
                position.status = 'reconcile_required'
                self._save()
                return False
            if position.pending_funding_venues and not defer_funding:
                try:
                    peers = [p for p in self.positions if p is not position and
                             p.symbol == position.symbol and p.status != 'closed']
                    if peers:
                        if not position.grid_cycle_id or self.reconcile_open_positions(0) != 'ok':
                            raise ValueError('网格剩余持仓未核对')
                    else:
                        for trader in (self.variational_trader, self.lighter_trader):
                            if trader.assert_flat(position.symbol) is not True:
                                raise ValueError('未确认空仓')
                except Exception:
                    self.halted_reason = '平仓后账户空仓或活动订单尚未确认，禁止新开仓'
                    position.status = 'reconcile_required'
                    self._save()
                    return False
                self.history.append('closed_pending_funding', symbol=position.symbol,
                    position_id=position.id, status='closed', funding_usdc=None,
                    reason='两腿回执及账户空仓已确认；资金费待补，净收益未知')
            position.status = 'closed'
            position.closed_time = fills_finished
            position.close_elapsed_ms = position.close_execution_ms
            position.accounting_pending = bool(position.pending_fee_venues or position.pending_funding_venues)
            position.close_accounting_ms = (None if position.accounting_pending else
                round((datetime.now() - close_started).total_seconds() * 1000))
            position.close_started_time = None
            self._save()
            for trader in (self.variational_trader, self.lighter_trader):
                cache = getattr(trader, '_account_snapshot_cache', None)
                if cache is not None:
                    cache.invalidate()
            return True

    def close_reason(self, position):
        self._close_signals.pop(position.id, None)
        try:
            reason = self._close_reason(position)
        except Exception as exc:
            if self.position_monitor:
                self.position_monitor.checked(position, error=exc)
            return None
        if self.position_monitor:
            self.position_monitor.checked(position, reason=reason)
        return reason

    @for_position
    def _close_reason(self, position):
        held_seconds = (datetime.now() - position.open_time).total_seconds()
        if self.close_max_hold_seconds > 0 and held_seconds > self.close_max_hold_seconds:
            self._close_signals[position.id] = dict(observed_at=utc_now(), reason='最长持仓保护',
                held_seconds=held_seconds, max_hold_seconds=self.close_max_hold_seconds)
            return '最长持仓保护'
        if position.grid_cycle_id:
            return self._grid_close_reason(position)
        try:
            mark_quotes = []
            if self.position_monitor:
                mark_quotes = tuple(quote_snapshot(quote) for quote in self.position_monitor.quotes(position))
                a, b = ((quote['bid'] + quote['ask']) / 2 for quote in mark_quotes)
            else:
                def price(trader):
                    getter = (getattr(trader, 'get_position_mark_snapshot')
                              if callable(getattr(type(trader), 'get_position_mark_snapshot', None)) else None)
                    if getter:
                        quote = quote_snapshot(getter(position.symbol))
                        mark_quotes.append(quote)
                        return (quote['bid'] + quote['ask']) / 2
                    mark_quotes.append({})
                    return trader.get_market_price(position.symbol)
                a, b = price(self.variational_trader), price(self.lighter_trader)
            observed_at = utc_now()
            pnl = self._calculate_net_mark_pnl(position, a, b)
            if self.max_loss_per_trade is not None and pnl <= -self.max_loss_per_trade:
                return '单笔亏损保护'
            current_spread_bps = abs(a - b) / max(abs(a), abs(b), 1e-12) * 10000
            entry_spread_bps = abs(position.entry_price_variational - position.entry_price_lighter) / max(
                abs(position.entry_price_variational), abs(position.entry_price_lighter), 1e-12) * 10000
            reduction_bps = max(0.0, entry_spread_bps - current_spread_bps)
            center, stats = None, {}
            effective_target = 'zero' if self.close_convergence_basis == 'remaining' else 'reduction'
            if position.entry_target:
                convergence_ok, target_current = self._target_convergence(position, mark_quotes)
                center = position.entry_target.get('center_bps')
                stats = {'samples': position.entry_target.get('samples', 0)}
                effective_target = 'frozen_' + position.entry_target['target_mode']
                current_spread_bps = target_current
            elif self.close_convergence_basis == 'remaining' and self.close_target_mode in ('center', 'either'):
                direction = 'buy_variational' if position.variational_side == 'long' else 'sell_variational'
                pair_key = self._route_key_for_direction(direction)
                stats = self._route_reversion_stats(position.symbol, pair_key, 'center')
                center = stats.get('center_bps')
                directional_current = self._directional_spread_bps(position, a, b)
                center_ok = (center is not None and directional_current is not None and
                             abs(float(directional_current) - float(center)) <= self.close_convergence_bps)
                # A fresh process may not have enough samples for a stable
                # historical center yet. Fall back to the existing zero band
                # so a position is never held forever just because history is
                # still warming up.
                if center is None:
                    center_ok = current_spread_bps <= self.close_convergence_bps
                effective_target = ('zero_fallback_missing_center' if center is None else
                                    'center_or_zero' if self.close_target_mode == 'either' else 'center')
                zero_ok = current_spread_bps <= self.close_convergence_bps
                convergence_ok = (center_ok or zero_ok) if self.close_target_mode == 'either' else center_ok
            else:
                convergence_ok = (current_spread_bps <= self.close_convergence_bps
                                  if self.close_convergence_basis == 'remaining'
                                  else reduction_bps >= self.close_convergence_bps)
                if self.close_target_mode == 'zero' and self.close_convergence_basis == 'remaining':
                    convergence_ok = current_spread_bps <= self.close_convergence_bps
            channel = (position.entry_target or {}).get('channel')
            exit_mode = 'convergence_and_profit' if channel else self.close_mode
            if exit_mode == 'disabled':
                return None
            if self.position_monitor:
                self.position_monitor.checks.setdefault(position.id, {}).update(
                    convergence_ok=convergence_ok, convergence_current_bps=current_spread_bps,
                    convergence_target_bps=channel['exit_axis_bps'] if channel else position.entry_target.get('target_bps') if position.entry_target else center)
            if exit_mode in ('convergence', 'convergence_and_profit') and not convergence_ok:
                return None
            # Midpoint convergence only selects a candidate exit. Every normal
            # single-pair exit must also pass executable prices, both closing
            # fees, settled funding and the same movement bounds sent to venues.
            def capture():
                self._capture_close_signal(position, '价差收敛', observed_at, mark_quotes, (a, b),
                    center=center, stats=stats, effective_target=effective_target,
                    convergence_ok=convergence_ok, current_spread_bps=current_spread_bps,
                    entry_spread_bps=entry_spread_bps, reduction_bps=reduction_bps)
            try:
                estimate = self._single_profit_estimate(position)
            except Exception as exc:
                if convergence_ok:
                    capture()
                    self._record_profit_wait(position, '已回归，平仓核验受阻：' + safe_reason(exc))
                raise
            profit_ok = self._single_profit_ok(estimate)
            if convergence_ok and not profit_ok:
                capture()
                self._record_profit_wait(position, '已回归，保护价净盈利未达标', estimate)
            if exit_mode == 'profit':
                reason = '盈利条件' if profit_ok else None
            elif exit_mode == 'convergence_and_profit':
                reason = '价差收敛且达到盈利' if convergence_ok and profit_ok else None
            elif exit_mode == 'convergence_or_profit':
                reason = ('价差收敛' if convergence_ok else '盈利条件') if profit_ok else None
            else:
                reason = '价差收敛' if convergence_ok and profit_ok else None
            if reason:
                self._capture_close_signal(position, reason, observed_at, mark_quotes, (a, b),
                    center=center, stats=stats, effective_target=effective_target,
                    convergence_ok=convergence_ok, current_spread_bps=current_spread_bps,
                    entry_spread_bps=entry_spread_bps, reduction_bps=reduction_bps)
            return reason
        except Exception:
            raise

    def should_close_position(self, position):
        return self.close_reason(position) is not None

    def _calculate_pnl(self, position, a, b):
        return ((a - position.entry_price_variational) * position.variational_size *
                (1 if position.variational_side == 'long' else -1) +
                (b - position.entry_price_lighter) * position.lighter_size *
                (1 if position.lighter_side == 'buy' else -1))

    def _calculate_net_mark_pnl(self, position, a, b):
        """Mark PnL after known entry fees, before final close settlement."""
        return self._calculate_pnl(position, a, b) - float(position.entry_fees_usdc or 0.0)

    def run_strategy_cycle(self, symbols):
        with self.lock:
            if self.live_blockers():
                return
            for position in self.positions:
                reason = self.close_reason(position) if position.status == 'open' else None
                if reason:
                    self.close_position(position, reason=reason)  # Failed closes remain in durable state.
            for symbol in symbols:
                opportunity = self.check_arbitrage_opportunity(symbol)
                if opportunity:
                    self.execute_arbitrage(opportunity)

    def get_next_trade_interval(self):
        return max(1, self.trade_interval_min)

    def reset_daily_stats(self):
        today = datetime.now().date().isoformat()
        if today != self.day:
            self.day, self.daily_trades, self.daily_pnl = today, 0, 0.0
            self._save()

    def get_strategy_status(self):
        with self.lock:
            active = [p for p in self.positions if p.status != 'closed']
            open_notional = self.open_position_notional()
            max_total_notional = float(self.max_total_position_notional or 0)
            return {'positions': len(active), 'daily_trades': self.daily_trades,
                    'daily_pnl': None if any(p.pending_funding_venues or p.accounting_pending for p in self.positions) else self.daily_pnl,
                    'known_pnl_subtotal': self.daily_pnl,
                    'pending_funding_count': sum(bool(p.pending_funding_venues) for p in self.positions),
                    'open_positions': [p.symbol for p in active],
                    'open_notional_usdc': open_notional,
                    'max_total_position_notional': max_total_notional,
                    'entry_limits': self.entry_limits_status(),
                    'remaining_open_notional': (max(0.0, max_total_notional - open_notional)
                                                if max_total_notional > 0 else None),
                    'halted_reason': self.halted_reason, 'live_ready': not self.live_blockers(),
                    'blockers': self.live_blockers(), 'mode': self.config.get('mode', 'monitor'),
                    'close_policy': {
                        'mode': self.close_mode,
                        'target_mode': self.close_target_mode,
                        'convergence_basis': self.close_convergence_basis,
                        'convergence_bps': self.close_convergence_bps,
                        'min_profit_usdc': self.close_min_profit_usdc,
                        'min_profit_bps': self.close_min_profit_bps,
                        'cost_reserve_usdc': self.close_cost_reserve_usdc,
                        'max_hold_seconds': self.close_max_hold_seconds,
                    },
                    'entry_policy': {
                        'mode': self.entry_mode,
                        'quantile': self.entry_quantile,
                        'min_samples': self.entry_min_samples,
                    },
                    'single_leg_recovery': {
                        'enabled': self.single_leg_recovery_enabled,
                        'max_attempts': self.single_leg_recovery_max_attempts,
                    },
                    'entry_scan_backoff_until': self.entry_scan_backoff_until.isoformat() if self.entry_scan_backoff_until else None,
                    'entry_scan_backoff_reason': self.entry_scan_backoff_reason}

    def record_entry_scan_failure(self, exc):
        """Pause entry discovery on server cooldowns without delaying open-position management."""
        from src.common.rate_limit import RateLimitError
        seconds = exc.retry_after_seconds if isinstance(exc, RateLimitError) else 0
        status = getattr(getattr(exc, 'response', None), 'status_code', None)
        if status is None:
            status = getattr(exc, 'status', None)
        if not seconds and (status == 429 or 'HTTP 429' in str(exc)):
            seconds = 10
        if not seconds:
            return
        until = datetime.now() + timedelta(seconds=seconds)
        with self.lock:
            if not self.entry_scan_backoff_until or until > self.entry_scan_backoff_until:
                self.entry_scan_backoff_until = until
                self.entry_scan_backoff_reason = f'平台接口限流，暂停新开仓扫描 {seconds} 秒；已有仓位继续监控'

    def entry_scan_backoff_seconds(self):
        if not self.entry_scan_backoff_until:
            return 0
        remaining = (self.entry_scan_backoff_until - datetime.now()).total_seconds()
        if remaining <= 0:
            self.entry_scan_backoff_until = None
            self.entry_scan_backoff_reason = None
            return 0
        return max(1, int(remaining))

    def entry_capacity_reason(self):
        """Cheap run-wide gates before scanning/preparing every candidate."""
        if self.halted_reason or any(p.status not in ('open', 'closed') for p in self.positions):
            return '待核对持仓或订单，暂停新开仓'
        if len([p for p in self.positions if p.status != 'closed']) >= self.max_positions:
            return '已达同时持仓数上限'
        if self.max_total_position_notional > 0 and self.entry_notional_cap() <= 0:
            return '已达最大总持仓金额上限'
        if self.day == datetime.now().date().isoformat() and self.daily_trades >= self.max_trades_per_day:
            return '已达每日尝试次数上限'
        if self.last_trade_time and (datetime.now() - self.last_trade_time).total_seconds() < self.trade_interval_min:
            return '尚未达到交易间隔'
        return None

    def entry_scan_status(self):
        until = self.entry_scan_backoff_until
        remaining = max(0, math.ceil((until - datetime.now()).total_seconds())) if until else 0
        feeds = {}
        accounts = {}
        traders = []
        for pair in self._route_trader_pairs or [(self.variational_trader, self.lighter_trader)]:
            for trader in pair:
                if trader is not None and not any(trader is item for item in traders):
                    traders.append(trader)
        for trader in traders:
            cache = getattr(trader, '_account_snapshot_cache', None)
            if cache is not None:
                accounts[self._venue_name(trader, 'unknown')] = cache.status()
            health = getattr(getattr(trader, '_reference_feed', None), 'health', None)
            if callable(health):
                state = health()
                if isinstance(state, dict):
                    feeds[self._venue_name(trader, 'unknown')] = state
        with self._entry_scan_lock:
            return dict(backoff_until=until.astimezone().isoformat() if remaining else None,
                capacity_block_reason=self.entry_capacity_reason(),
                backoff_reason=self.entry_scan_backoff_reason if remaining else None,
                backoff_remaining_seconds=remaining, preview_skipped=self._entry_preview_skips,
                last_preview_skip=copy.deepcopy(self._last_entry_preview_skip),
                last_sized_rejection=copy.deepcopy(getattr(self, '_last_sized_rejection', None)),
                cached_scan=copy.deepcopy(self._last_cached_scan), reference_feeds=feeds,
                account_snapshots=accounts)

    def _position_mark(self, position, read_market=True):
        """Return a best-effort live mark for one open hedge.

        This is deliberately read-only.  A transient mark failure leaves the
        position visible with an explicit unknown estimate rather than making
        the UI look flat or inventing a zero PnL.
        """
        actual_spread = position.actual_entry_spread_bps
        if actual_spread is None:
            actual_spread = self._actual_entry_spread_bps(position)
        row = {
            'id': position.id,
            'position_mode': 'spread_grid' if position.grid_cycle_id else 'single',
            'grid_cycle_id': position.grid_cycle_id,
            'grid_layer': position.grid_layer + 1 if position.grid_layer is not None else None,
            'grid_entry_net_bps': position.grid_entry_net_bps,
            'entry_target': copy.deepcopy(position.entry_target),
            'target_entry_net_bps': position.target_entry_net_bps,
            'symbol': position.symbol,
            'status': position.status,
            'open_time': position.open_time.isoformat(),
            'variational_side': position.variational_side,
            'lighter_side': position.lighter_side,
            'entry_price_variational': position.entry_price_variational,
            'entry_price_lighter': position.entry_price_lighter,
            'entry_accounting': copy.deepcopy(position.entry_accounting),
            'variational_size': position.variational_size,
            'lighter_size': position.lighter_size,
            'entry_size_variational': position.entry_size_variational or position.variational_size,
            'entry_size_lighter': position.entry_size_lighter or position.lighter_size,
            'execution_id': position.execution_id,
            'execution_elapsed_ms': position.execution_elapsed_ms,
            'close_started_time': position.close_started_time.isoformat() if position.close_started_time else None,
            'close_elapsed_ms': position.close_elapsed_ms,
            'close_execution_ms': position.close_execution_ms,
            'close_accounting_ms': position.close_accounting_ms,
            'close_reason': position.close_reason,
            'manual_close': copy.deepcopy(position.manual_close),
            'rollback_category': position.rollback_category,
            'close_price_variational': position.close_price_variational,
            'close_price_lighter': position.close_price_lighter,
            'actual_entry_spread_bps': actual_spread,
            'variational_venue': position.variational_venue,
            'lighter_venue': position.lighter_venue,
            'accounting_pending': bool(position.accounting_pending),
            'pending_funding_venues': list(position.pending_funding_venues),
            'pending_fee_venues': list(position.pending_fee_venues),
            'funding_settlements': dict(position.funding_settlements),
            'hedge_status': ('paired' if position.entry_size_variational and position.entry_size_lighter
                            else 'single_leg_recovered'),
        }
        partially_closed = any(
            getattr(position, 'entry_size_' + leg) > getattr(position, leg + '_size') + 1e-8
            for leg in ('variational', 'lighter'))
        row['partially_closed'] = partially_closed
        if partially_closed:
            row['known_realized_pnl_usdc'] = position.realized_pnl_usdc
        row.update({'mark_price_variational': None, 'mark_price_lighter': None,
                    'unrealized_pnl_usdc': None, 'current_spread_bps': None,
                    'mark_updated_at': None, 'mark_status': 'unavailable',
                    'mark_note': '按双腿盘口中间价估算，已扣已知入场手续费；未扣未来平仓手续费和待结算资金费。'})
        if not read_market:
            return row
        try:
            traders = dict(zip(('variational', 'lighter'), self.position_traders(position)))
        except ValueError as exc:
            row['mark_error'] = str(exc)
            return row
        errors, snapshots, last_snapshots = {}, {}, {}
        def snapshot(trader):
            getter = getattr(trader, 'get_mark_snapshot', None)
            result = getter(position.symbol) if getter else {
                'price': trader.get_market_price(position.symbol),
                'updated_at': datetime.now().astimezone().isoformat()}
            value = result['price']
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError('标记价无效')
            return result
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {leg: pool.submit(snapshot, trader) for leg, trader in traders.items()}
            for leg, future in futures.items():
                try:
                    snapshots[leg] = future.result()
                    row['mark_price_' + leg] = float(snapshots[leg]['price'])
                except Exception as exc:
                    reason = getattr(exc, 'display_reason', None)
                    if not reason:
                        reason = ('行情限流，等待新报价' if 'RateLimit' in type(exc).__name__
                                  else '行情暂不可用')
                    errors[row.get(leg + '_venue') or leg] = reason
                    last_getter = getattr(traders[leg], 'get_last_mark_snapshot', None)
                    if last_getter:
                        last_value = last_getter(position.symbol)
                        if isinstance(last_value, dict):
                            last_snapshots[leg] = last_value
        if errors:
            row.update({'mark_errors': errors, 'mark_error': '；'.join(
                venue + '：' + reason for venue, reason in errors.items())})
        else:
            a, b = row['mark_price_variational'], row['mark_price_lighter']
            row.update({'current_spread_bps': self._directional_spread_bps(position, a, b),
                        'mark_updated_at': min(snapshots.values(), key=lambda r:
                            datetime.fromisoformat(r['updated_at']))['updated_at'],
                        'mark_sources': {leg: value.get('source') for leg, value in snapshots.items()},
                        'mark_status': 'live'})
        if partially_closed:
            row['mark_note'] = '部分已平仓：合并已知已实现损益与剩余持仓浮盈亏；未扣未来平仓手续费和待结算资金费。当前价差仅为原路线行情参考。'
        # A flat leg still needs a quote for the route's current spread, but
        # cannot block marking the real remaining exposure on the other venue.
        needed = [leg for leg in ('variational', 'lighter') if getattr(position, leg + '_size') > 1e-8]
        if all(leg in snapshots for leg in needed):
            a = row['mark_price_variational'] or position.entry_price_variational
            b = row['mark_price_lighter'] or position.entry_price_lighter
            if partially_closed:
                # The realized subtotal already includes entry fees and
                # confirmed partial closes; include it without charging twice.
                if position.realized_pnl_usdc is None or position.pending_fee_venues:
                    row['pnl_error'] = '部分已平仓，已实现损益或手续费待核对'
                else:
                    row['unrealized_pnl_usdc'] = self._calculate_pnl(position, a, b) + position.realized_pnl_usdc
            elif not errors:
                row['unrealized_pnl_usdc'] = self._calculate_net_mark_pnl(position, a, b)
        # Exit estimate uses the correct closing side and verified fee rates.
        # It is a BBO estimate; final execution still checks same-size depth.
        row['exit_estimate_note'] = '按平仓方向买卖价与已核验费率估算；未计额外盘口冲击和待结算资金费。'
        if not partially_closed and not errors:
            try:
                if position.pending_fee_venues:
                    raise ValueError('entry fees unknown')
                exit_prices, exit_fee = {}, 0.0
                for leg in ('variational', 'lighter'):
                    quote = snapshots[leg]
                    side = getattr(position, leg + '_side')
                    price = quote['bid' if self._snapshot_side(side) == 'long' else 'ask']
                    fee = quote['fee_bps']
                    if (not isinstance(fee, (int, float)) or isinstance(fee, bool) or
                            not math.isfinite(fee) or not 0 <= fee < 10000):
                        raise ValueError('fee unknown')
                    exit_prices[leg] = price
                    exit_fee += price * getattr(position, leg + '_size') * fee / 10000
                row['estimated_exit_fee_usdc'] = exit_fee
                row['estimated_exit_pnl_usdc'] = self._calculate_net_mark_pnl(
                    position, exit_prices['variational'], exit_prices['lighter']) - exit_fee
            except (KeyError, TypeError, ValueError):
                row['exit_estimate_note'] = '平仓估算待补双腿买卖价或核验费率。'
        if errors and not partially_closed:
            previous = {**last_snapshots, **snapshots}
            if all(leg in previous for leg in ('variational', 'lighter')):
                a, b = (previous[leg]['price'] for leg in ('variational', 'lighter'))
                row['last_estimate'] = {
                    'unrealized_pnl_usdc': self._calculate_net_mark_pnl(position, a, b),
                    'spread_bps': self._directional_spread_bps(position, a, b),
                    'prices': [a, b], 'updated_at': min(value['updated_at'] for value in previous.values())}
        return row

    def _closed_position_row(self, position, close_timing=None, receipt_prices=None):
        receipt_prices = receipt_prices or {}
        def recovered_price(legacy_name, concrete_name, stored):
            if stored is not None:
                return stored
            # Once receipts use real venue names, ``lighter`` can mean the
            # FIRST leg of a Lighter/Arcus pair. Do not reuse that close fill
            # as the historical ``lighter_*`` (second) leg's Arcus price.
            names = ([legacy_name, concrete_name] if 'variational' in receipt_prices
                     else [concrete_name or legacy_name])
            for name in names:
                value = receipt_prices.get(name)
                if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
                    return float(value)
            return None
        close_price_variational = recovered_price(
            'variational', position.variational_venue, position.close_price_variational)
        close_price_lighter = recovered_price(
            'lighter', position.lighter_venue, position.close_price_lighter)
        actual_spread = position.actual_entry_spread_bps
        if actual_spread is None:
            actual_spread = self._actual_entry_spread_bps(position)
        close_spread = self._directional_spread_bps(
            position, close_price_variational, close_price_lighter)
        close_elapsed = position.close_elapsed_ms
        if close_elapsed is None and close_timing and position.closed_time:
            try:
                local_tz = datetime.now().astimezone().tzinfo
                started = datetime.fromisoformat(close_timing['first_receipt_at']).astimezone(local_tz)
                ended = position.closed_time.replace(tzinfo=local_tz)
                close_elapsed = max(0, round((ended - started).total_seconds() * 1000))
            except (KeyError, TypeError, ValueError, ArithmeticError):
                close_elapsed = None
        close_reason = position.close_reason
        if not close_reason and position.closed_time:
            held_seconds = (position.closed_time - position.open_time).total_seconds()
            if (position.entry_size_variational and position.entry_size_lighter and
                    self.close_max_hold_seconds > 0 and
                    held_seconds >= self.close_max_hold_seconds * 0.9):
                close_reason = '最长持仓保护（旧记录推断）'
            elif not (position.entry_size_variational and position.entry_size_lighter):
                close_reason = '第二腿未成对·回补第一腿（旧记录推断）'
        return {
            'id': position.id,
            'symbol': position.symbol,
            'position_mode': 'spread_grid' if position.grid_cycle_id else 'single',
            'grid_cycle_id': position.grid_cycle_id,
            'grid_layer': position.grid_layer + 1 if position.grid_layer is not None else None,
            'grid_entry_net_bps': position.grid_entry_net_bps,
            'entry_target': copy.deepcopy(position.entry_target),
            'target_entry_net_bps': position.target_entry_net_bps,
            'status': position.status,
            'open_time': position.open_time.isoformat(),
            'closed_time': position.closed_time.isoformat() if position.closed_time else None,
            'variational_side': position.variational_side,
            'lighter_side': position.lighter_side,
            'entry_price_variational': position.entry_price_variational,
            'entry_price_lighter': position.entry_price_lighter,
            'close_price_variational': close_price_variational,
            'close_price_lighter': close_price_lighter,
            'entry_size_variational': position.entry_size_variational,
            'entry_size_lighter': position.entry_size_lighter,
            'execution_id': position.execution_id,
            'execution_elapsed_ms': position.execution_elapsed_ms,
            'close_elapsed_ms': close_elapsed,
            'close_execution_ms': position.close_execution_ms,
            'close_accounting_ms': position.close_accounting_ms,
            'close_reason': close_reason,
            'rollback_category': position.rollback_category,
            'actual_entry_spread_bps': actual_spread,
            'close_spread_bps': close_spread,
            'variational_venue': position.variational_venue,
            'lighter_venue': position.lighter_venue,
            'realized_pnl_usdc': position.realized_pnl_usdc,
            'entry_fees_usdc': position.entry_fees_usdc or 0.0,
            'accounting_pending': bool(position.accounting_pending),
            'pending_funding_venues': list(position.pending_funding_venues),
            'pending_fee_venues': list(position.pending_fee_venues),
            'funding_settlements': dict(position.funding_settlements),
            'hedge_status': ('paired' if position.entry_size_variational and position.entry_size_lighter
                            else 'single_leg_recovered'),
        }

    def get_execution_overview(self, mark_open=True):
        """Build the user-facing execution book without claiming settlement."""
        with self.lock:
            open_positions = [self._position_mark(p, read_market=mark_open) for p in self.positions if p.status != 'closed']
            closed = [p for p in self.positions if p.status == 'closed' and (p.entry_size_variational or p.entry_size_lighter)]
            close_timings = self.history.close_timings([p.id for p in closed])
            receipt_orders = self.history.receipt_orders([p.id for p in self.positions])
            receipt_prices = self.history.receipt_prices([p.id for p in closed])
            closed_positions = []
            for position in closed:
                row = self._closed_position_row(
                    position, close_timings.get(position.id), receipt_prices.get(position.id))
                receipts = receipt_orders.get(position.id, {})
                row['entry_order_ids'] = list(receipts.get('entry', []))
                row['close_order_ids'] = list(receipts.get('close', []))
                row['entry_order_ids_by_venue'] = dict(receipts.get('entry_by_venue', {}))
                row['close_order_ids_by_venue'] = dict(receipts.get('close_by_venue', {}))
                closed_positions.append(row)
            for row in open_positions:
                receipts = receipt_orders.get(row['id'], {})
                row['entry_order_ids'] = list(receipts.get('entry', []))
                row['close_order_ids'] = list(receipts.get('close', []))
                row['entry_order_ids_by_venue'] = dict(receipts.get('entry_by_venue', {}))
                row['close_order_ids_by_venue'] = dict(receipts.get('close_by_venue', {}))
            # Keep every consumer's default order stable: newest activity
            # first, with open rows ordered by their entry time and closed
            # rows by the actual close time.
            open_positions.sort(key=lambda row: row.get('open_time') or '', reverse=True)
            closed_positions.sort(
                key=lambda row: row.get('closed_time') or row.get('open_time') or '',
                reverse=True,
            )
            unrealized_values = [p['unrealized_pnl_usdc'] for p in open_positions
                                 if isinstance(p.get('unrealized_pnl_usdc'), (int, float))]
            realized_values = [p['realized_pnl_usdc'] for p in closed_positions
                               if isinstance(p.get('realized_pnl_usdc'), (int, float))]
            realized = self.daily_pnl if self.daily_pnl is not None else None
            unrealized = sum(unrealized_values) if len(unrealized_values) == len(open_positions) else None
            total = (realized + unrealized) if realized is not None and unrealized is not None else None
            return {
                'open_positions': open_positions,
                'closed_positions': closed_positions,
                'position_policy': self.grid_status(),
                'summary': {
                    'open_count': len(open_positions),
                    'closed_count': len(closed_positions),
                    'paired_closed_count': sum(1 for p in closed_positions if p.get('hedge_status') == 'paired'),
                    'single_leg_recovered_count': sum(1 for p in closed_positions if p.get('hedge_status') == 'single_leg_recovered'),
                    'realized_pnl_usdc': realized,
                    'estimated_unrealized_pnl_usdc': unrealized,
                    'estimated_total_pnl_usdc': total,
                    'realized_position_rows': sum(realized_values) if realized_values else None,
                    'pending_accounting_count': sum(1 for p in self.positions
                                                    if p.accounting_pending or p.pending_funding_venues),
                    'mark_complete': len(unrealized_values) == len(open_positions),
                },
            }
