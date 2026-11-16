"""Shared entry/exit target policy, with legacy-position compatibility."""
import copy
import math
import time

from src.common.market_data import age_seconds, utc_now
from src.execution.target_return import directed_spread, exit_projection, projected_return, finite
from src.execution.return_preview import choose_target, preview_policy, round_trip_preview
from src.execution.funding_forecast import funding_scenario


class TargetReturnMixin:
    @property
    def channel_enabled(self):
        return self.entry_mode == 'channel'

    def _channel_entry_context(self, symbol, direction, quotes, frozen=None):
        from src.execution.channel_policy import channel_policy, channel_bounds, channel_context
        if not quotes or not all(isinstance(q,dict) for q in quotes):
            raise ValueError('通道入场需要双平台有效买卖价')
        p = channel_policy(self.config)
        venues = (self.variational_trader.venue,self.lighter_trader.venue)
        canonical = venues[0] < venues[1]
        a,b = quotes if canonical else tuple(reversed(quotes))
        axis = sorted(venues)
        upper = (direction == 'buy_variational') == canonical
        if frozen is not None:
            ch = frozen.get('channel') or {}
            if (frozen.get('symbol')!=symbol or frozen.get('route')!=self._route_key_for_direction(direction)
                    or ch.get('axis_venues') != axis or ch.get('side') != ('upper' if upper else 'lower')):
                raise ValueError('冻结通道与平台方向不一致')
            # Recheck only the entry boundary before the FIRST submission;
            # targets remain frozen and completion may consume entry reserve.
            current = directed_spread(a['ask'],b['bid'])
            if (current < ch['upper_bps'] if upper else current > ch['lower_bps']):
                raise ValueError('最新双平台报价已离开通道入场边界')
            return copy.deepcopy(frozen)
        stats = {}
        if p['method'] != 'fixed':
            from src.api_v2.center_statistics import center_stats_for
            stats = center_stats_for(self.market_history,self._history_symbol(symbol),
                '->'.join(axis),p['window_seconds'],p['quantile'])
        bounds = channel_bounds(p,stats)
        self._channel_cycle_gate(symbol, axis, upper, directed_spread(a['ask'],b['bid']), bounds, p)
        context = channel_context(p,bounds,a,b,upper_direction=upper,symbol=symbol,
            route=self._route_key_for_direction(direction),axis_venues=axis)
        context['channel']['close_policy'] = copy.deepcopy(self.config.get('close_policy',{}))
        context.update(frozen_at=utc_now(),funding_reserve_bps=finite(
            self.config.get('entry_policy',{}).get('funding_reserve_bps'),'整轮资金费预算',0))
        return context

    def _channel_cycle_gate(self, symbol, axis, upper, current, bounds, policy):
        # One route cycle per symbol. Reversal never opens against an old
        # group, including after restart or manual partial close.
        if any(p.symbol==symbol and p.status!='closed' for p in self.positions):
            raise ValueError('该品种仍有未结束持仓，通道反向需先确认双边结束')
        previous = next((p for p in reversed(self.positions) if p.symbol==symbol and p.status=='closed'
            and p.entry_size_variational and p.entry_size_lighter and p.entry_target
            and (p.entry_target.get('channel') or {}).get('axis_venues')==axis),None)
        if previous is None:
            return
        old = previous.entry_target['channel']
        side = 'upper' if upper else 'lower'
        if policy['exit_mode']=='opposite' and old.get('exit_mode')=='opposite' and previous.close_reason in (
                '价差收敛','盈利条件','价差收敛且达到盈利'):
            if old.get('side')==side:
                raise ValueError('对侧循环等待反向边界；反向净收益仍须达标')
        elif old.get('side')==side:
            # Memory-only rearming. On restart wait for one observed return
            # inside the entry boundary; never repeatedly enter one excursion.
            armed = getattr(self,'_channel_rearmed',set())
            if (current < bounds['upper_bps'] if upper else current > bounds['lower_bps']):
                armed.add(previous.id)
            self._channel_rearmed = armed
            if previous.id not in armed:
                raise ValueError('同侧通道等待回到边界内后再次触边')

    @property
    def target_return_enabled(self):
        # Version is persisted on migration, not exposed as a strategy switch.
        return self.config.get('entry_policy', {}).get('calculation_version', 1) == 2

    def _target_policy_stats(self, symbol, direction):
        window = int(self.config.get('entry_policy', {}).get('target_window_seconds', 604800))
        key = (self._history_symbol(symbol), self._route_key_for_direction(direction), 'target', window, self.entry_quantile)
        cached = self._market_stats_cache.get(key)
        if cached and time.monotonic() - cached[0] < (30 if cached[1].get('center_ready', True) else 1):
            return cached[1]
        from src.api_v2.center_statistics import center_stats_for
        stats = center_stats_for(self.market_history, key[0], key[1], window, self.entry_quantile)
        self._market_stats_cache[key] = (time.monotonic(), stats)
        if len(self._market_stats_cache) > 2048:
            oldest = min(self._market_stats_cache, key=lambda k: self._market_stats_cache[k][0])
            self._market_stats_cache.pop(oldest, None)
        return stats

    def _entry_target_context(self, symbol, direction, current_bps, frozen=None, quotes=None):
        route = self._route_key_for_direction(direction)
        if self.channel_enabled and (frozen is None or frozen.get('target_mode')=='channel'):
            return self._channel_entry_context(symbol,direction,quotes,frozen)
        if frozen is None and self.grid_enabled:
            active = self._grid_active()
            if active:
                if any(p.symbol != symbol or p.grid_direction != direction or not p.entry_target for p in active):
                    raise ValueError('网格本轮回归目标或方向不一致，禁止加仓')
                frozen = active[0].entry_target
        if frozen is not None:
            if frozen.get('symbol') != symbol or frozen.get('route') != route:
                raise ValueError('已冻结回归目标与本次方向不一致')
            finite(frozen.get('target_bps'), '已冻结回归目标')
            finite(frozen.get('funding_reserve_bps'), '整轮资金费预算', 0)
            return copy.deepcopy(frozen)
        mode = self.close_target_mode
        stats = self._target_policy_stats(symbol, direction) if mode in ('center', 'either') else {}
        center = stats.get('center_bps')
        if mode in ('center', 'either') and (center is None or int(stats.get('samples') or 0) < 30):
            raise ValueError('历史中心样本不足30个，等待；不会自动改用零目标')
        if mode in ('center', 'either') and stats.get('center_ready') is False:
            raise ValueError(stats.get('center_reason') or '历史中心有效样本待更新')
        # Legacy "either" configurations choose the nearest favorable target
        # once. They do not switch targets after the first order is submitted.
        effective, target = choose_target(mode, center, stats.get('samples', 0), current_bps)
        reserve = finite(self.config.get('entry_policy', {}).get('funding_reserve_bps'), '整轮资金费预算', 0)
        return dict(version=2, symbol=symbol, route=route, target_mode=effective, target_policy_mode=mode, target_bps=target,
                    center_bps=center, samples=int(stats.get('samples') or 0),
                    window_seconds=int(self.config.get('entry_policy', {}).get('target_window_seconds', 604800)),
                    frozen_at=utc_now(), funding_reserve_bps=reserve)

    def _target_models(self, symbol, v, l, opening_impacts, closing, frozen=None):
        models, errors = {}, {}
        for direction in ('buy_variational', 'sell_variational'):
            try:
                buy_q, sell_q = (v, l) if direction == 'buy_variational' else (l, v)
                buy_leg, sell_leg = (('variational', 'lighter') if direction == 'buy_variational'
                                     else ('lighter', 'variational'))
                direction_frozen = (frozen.get(direction) if frozen is not None and 'target_bps' not in frozen else frozen)
                context = self._entry_target_context(symbol, direction,
                    directed_spread(buy_q['ask'], sell_q['bid']), direction_frozen, quotes=(v,l))
                # Freeze the selected funding scenario alongside the target.
                # Subsequent re-quotes and hedge completion cannot spend a
                # different funding budget after the first leg is sent.
                if direction_frozen is None and not (self.grid_enabled and self._grid_active()):
                    funding = funding_scenario(buy_q, sell_q, preview_policy(self.config))
                    context.update(funding_reserve_bps=finite(funding['budget_bps'], '整轮资金费预算', 0),
                                   funding_forecast=funding)
                projection = exit_projection(buy_q, sell_q, context['target_bps'],
                    closing[direction][buy_leg], closing[direction][sell_leg], context['funding_reserve_bps'],
                    max(float(buy_q['ask']), float(sell_q['ask'])) * (1 + max(opening_impacts[direction].values()) / 10000))
                models[direction] = dict(context=context, projection=projection,
                    buy_fee_bps=buy_q['fee_bps'], sell_fee_bps=sell_q['fee_bps'],
                    opening_buy_impact_bps=opening_impacts[direction]['buy'],
                    opening_sell_impact_bps=opening_impacts[direction]['sell'])
            except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
                errors[direction] = str(exc)
        return models, errors

    @staticmethod
    def _target_edge(models, direction, buy, sell, quantity, second_depth_in_price=False):
        model = models.get(direction)
        if model is None:
            return dict(net_edge_bps=-100000.0, estimated_entry_edge_usdc=-1e100)
        buy_impact, sell_impact = model['opening_buy_impact_bps'], model['opening_sell_impact_bps']
        # The allocator's Lighter price is already its depth VWAP. BBO-based
        # evaluations still need both impacts, but allocation must not add
        # that same depth impact a second time.
        if second_depth_in_price:
            if direction == 'buy_variational':
                sell_impact = 0.0
            else:
                buy_impact = 0.0
        return projected_return(buy, sell, quantity, model['buy_fee_bps'], model['sell_fee_bps'],
            buy_impact, sell_impact, model['projection'])

    def _target_profit_floor_usdc(self, quantity, projection):
        # Exit bps use BOTH closing notionals; entry bps use one larger leg.
        closing_notional = quantity * (projection['close_sell_price'] + projection['close_buy_price'])
        return max(self.close_min_profit_usdc + self.close_cost_reserve_usdc,
                   self.close_min_profit_bps * closing_notional / 10000)

    def _target_floor_bps(self, quantity, reference, projection, reserve=False):
        amount_floor = self._target_profit_floor_usdc(quantity, projection) / (quantity * reference) * 10000
        floor = max(float(self.spread_threshold_min), amount_floor)
        return floor + (self.execution_safety_margin_bps if reserve else 0)

    def _completion_target_projection(self, selected, first_quote, second_quote, first_fill_price):
        direction = selected['direction']
        context = selected['target_context']
        buy, sell = ((first_quote, second_quote) if direction == 'buy_variational'
                     else (second_quote, first_quote))
        buy_leg, sell_leg = (('variational', 'lighter') if direction == 'buy_variational'
                             else ('lighter', 'variational'))
        return exit_projection(buy, sell, context['target_bps'],
            selected['close_slippage_bps'][buy_leg], selected['close_slippage_bps'][sell_leg],
            context['funding_reserve_bps'], max(first_fill_price,
                selected['target_projection']['funding_reference_price'],
                max(float(buy['ask']), float(sell['ask'])) *
                    (1 + max(selected['buy_slippage_bps'], selected['sell_slippage_bps']) / 10000)))

    def _target_convergence(self, position, quotes):
        context = position.entry_target
        if not context:
            raise ValueError('持仓缺少冻结回归目标')
        if len(quotes) != 2:
            raise ValueError('回归判断缺少双平台买卖价')
        for quote in quotes:
            age = age_seconds(quote.get('updated_at'))
            if age is None or not 0 <= age <= 5:
                raise ValueError('回归判断报价过期')
        channel = context.get('channel')
        if channel:
            by_venue = dict(zip((position.variational_venue, position.lighter_venue),quotes))
            a,b = (by_venue[v] for v in channel['axis_venues'])
            current = directed_spread(a['ask'],b['bid'])
            tolerance = channel.get('close_policy',{}).get('convergence_bps',self.close_convergence_bps)
            target = finite(channel.get('exit_axis_bps'),'通道平仓目标')
            return (current <= target+tolerance if channel['side']=='upper' else current >= target-tolerance),current
        buy, sell = quotes if position.variational_side == 'long' else tuple(reversed(quotes))
        current = directed_spread(buy['ask'], sell['bid'])
        target = finite(context.get('target_bps'), '持仓回归目标')
        # Every target entry starts above its directed target. Crossing it is
        # convergence too; a price jump must not skip the exit band forever.
        return current <= target + self.close_convergence_bps, current

    def target_discovery_rank(self, opportunity):
        direction = ('sell_variational' if opportunity.spread_direction == 'variational_higher'
                     else 'buy_variational')
        context = opportunity.target_context
        deviation = float(opportunity.spread_bps) - context['target_bps']
        preview = getattr(opportunity, 'return_preview', None) or {}
        net = preview.get('net_bps')
        recency = self.reversion_recency_rank(opportunity.symbol, direction)
        if isinstance(net, (int, float)) and math.isfinite(net):
            # Complete four-side BBO estimate outranks an uncosted midpoint
            # deviation. Final sized preparation decides executable ranking.
            return (3 if preview.get('meets_floor') else 2, preview['profit_usdc'],
                    -float(preview.get('quote_age_seconds', math.inf)), net, recency, context.get('samples', 0), context['route'])
        return (1, deviation * self.entry_notional_cap() / 10000, -math.inf, deviation,
                recency, context.get('samples', 0), context['route'])

    def discovery_return_preview(self, symbol, direction, context, quotes):
        if not all(isinstance(q, dict) for q in quotes):
            return None
        buy, sell = quotes if direction == 'buy_variational' else tuple(reversed(quotes))
        policy = preview_policy(self.config)
        policy['movement_bps'] = self.execution_entry_movement_bps(symbol, direction)
        policy['movement_evidence'] = self.execution_window_risk.evidence(self._route_guard_key(symbol, direction))
        cap = self.entry_notional_cap()
        learned = self.execution_window_risk.size_cap(self._route_guard_key(symbol, direction))
        cap = min(cap, learned or cap)
        quantity = cap / max(float(q['ask']) for q in quotes)
        converted = []
        for q in (buy, sell):
            q = dict(q)
            book = q.pop('book', None)
            for side, levels, size in (('buy', 'asks', 'ask_size'), ('sell', 'bids', 'bid_size')):
                q[side+'_impact_bps'] = (self._book_slippage_bps(book, levels, quantity) if book else
                    0.0 if isinstance(q.get(size), (int, float)) and q[size] >= quantity else None)
            converted.append(q)
        result = round_trip_preview(*converted, quantity, context['target_mode'],
                                    context.get('center_bps'), context.get('samples'), policy,
                                    target_override=context['target_bps'] if context.get('channel') else None)
        ages = [age_seconds(q.get('updated_at')) for q in quotes]
        result['quote_age_seconds'] = max(ages) if all(a is not None and a >= 0 for a in ages) else math.inf
        # A cheap upper bound, not an execution approval. Even a smaller order
        # cannot beat the fresh best bid/ask. Ignore ALL depth impact, movement
        # and funding costs here; keep the final sized gate fully conservative.
        # Legacy "either" may select a different target on sized re-quote.
        # Stale quotes, flexible targets and grid cycles must not prune here.
        if not self.grid_enabled and context.get('target_policy_mode') != 'either' and all(
                (age := age_seconds(q.get('updated_at'))) is not None and 0 <= age <= 2
                and type(q.get('bid')) in (int, float) and type(q.get('ask')) in (int, float)
                and 0 < q['bid'] <= q['ask'] for q in quotes):
            optimistic_quotes = [dict(q, buy_impact_bps=0., sell_impact_bps=0.) for q in converted]
            optimistic_policy = dict(policy, movement_bps=0., funding_mode='fixed', funding_reserve_bps=0.)
            optimistic = round_trip_preview(*optimistic_quotes, quantity, context['target_mode'],
                context.get('center_bps'), context.get('samples'), optimistic_policy,
                target_override=context['target_bps'] if context.get('channel') else None)
            if type(optimistic.get('net_bps')) in (int, float) and math.isfinite(optimistic['net_bps']):
                result['optimistic_net_bps'] = optimistic['net_bps']
                # The base floor is a lower bound on the final sized floor.
                # Rounding down quantity can only increase an absolute floor.
                result['optimistic_minimum_bps'] = policy['base_min_bps'] + policy['entry_reserve_bps']
        return result

    def discovery_preview_allowed(self, symbol, direction, preview):
        preview = preview or {}
        net, floor = preview.get('optimistic_net_bps'), preview.get('optimistic_minimum_bps')
        if not all(type(v) in (int, float) and math.isfinite(v) for v in (net, floor)) or net + 1e-6 >= floor:
            return True
        # Memory only: no SQLite writes or extra network calls on the scan path.
        with self._entry_scan_lock:
            self._entry_preview_skips += 1
            self._last_entry_preview_skip = dict(symbol=symbol, route=self._route_key_for_direction(direction),
                net_bps=preview.get('net_bps'), optimistic_net_bps=net, minimum_bps=floor, updated_at=utc_now())
        return False
