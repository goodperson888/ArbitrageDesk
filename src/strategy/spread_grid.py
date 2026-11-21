"""One route per grid cycle, durable layers and executable exit estimates."""
import math
import uuid
import time
from datetime import datetime

from src.execution.grid_policy import GridPolicy, layer_funding
from src.execution.orders import fresh_quote
from src.execution.exit_limits import allocate_close_movement
from src.common.market_data import utc_now
from src.strategy.position_routes import for_position


class SpreadGridMixin:
    def _load_position_policy(self):
        self.grid_policy = GridPolicy.from_config(self.config)

    @property
    def grid_enabled(self):
        return self.grid_policy.mode == 'spread_grid'

    def _grid_active(self):
        return [p for p in self.positions if p.status != 'closed']

    def _grid_context(self, symbol, direction):
        if not self.grid_enabled:
            return None
        from src.execution.precheck import EntryPrecheckMixin
        if any(not isinstance(t, EntryPrecheckMixin) or
               getattr(t, 'supports_grid_additions', False) is not True
               for t in (self.variational_trader, self.lighter_trader)):
            raise ValueError('价差网格仅支持已核验的 Arcus / Lighter 执行腿')
        active = self._grid_active()
        if not active:
            return {'cycle_id': uuid.uuid4().hex, 'layer': 0, 'anchor_bps': None,
                    'hedge_minimum_bps': max(0.0, float(self.spread_threshold_min)),
                    'minimum_bps': self.execution_min_bps(symbol, direction)}
        pair = (self.variational_trader, self.lighter_trader)
        if any(self.position_traders(p) != pair for p in active):
            raise ValueError('网格本轮平台组合不一致，禁止加仓')
        if any(p.symbol != symbol or not p.grid_cycle_id or p.status != 'open' or
               p.grid_direction != direction for p in active):
            raise ValueError('本轮网格已有其他品种、方向或未完成层，等待整组结束')
        cycle = active[0].grid_cycle_id
        if any(p.grid_cycle_id != cycle for p in active):
            raise ValueError('网格轮次不一致，需核对')
        peers = [p for p in self.positions if p.grid_cycle_id == cycle and
                 (p.entry_size_variational or p.entry_size_lighter)]
        layer = max(p.grid_layer for p in peers) + 1
        if layer >= self.grid_policy.max_layers:
            raise ValueError('本轮网格格位已用完，等待整组结束')
        anchor = active[0].grid_anchor_bps
        completed = [p.grid_entry_net_bps for p in peers if p.grid_entry_net_bps is not None]
        if anchor is None or not completed:
            raise ValueError('网格首层实际净价差尚未确认')
        hedge_minimum = max(anchor + layer * self.grid_policy.step_bps,
                            max(completed) + self.grid_policy.step_bps,
                            float(self.spread_threshold_min))
        minimum = max(hedge_minimum, self.execution_min_bps(symbol, direction))
        if self.target_return_enabled:
            minimum = hedge_minimum + self.execution_safety_margin_bps
        return {'cycle_id': cycle, 'layer': layer, 'anchor_bps': anchor,
                'minimum_bps': minimum, 'hedge_minimum_bps': hedge_minimum}

    def _grid_cap(self):
        if not self.grid_enabled:
            return float('inf')
        # Count the larger remaining leg, including a partial/unknown layer.
        used = sum(max(abs(p.variational_size * p.entry_price_variational),
                       abs(p.lighter_size * p.entry_price_lighter)) for p in self._grid_active())
        return max(0, min(self.grid_policy.layer_notional_usdc,
                          self.grid_policy.total_notional_usdc - used))

    def _grid_entry_evidence(self, symbol):
        if not self.grid_enabled:
            return {}
        active = self._grid_active()
        if not active:
            return {}
        # Do not authorize an increase until both native account readers
        # agree with every known remaining layer.
        if any(p.symbol != symbol or p.status != 'open' for p in active):
            raise ValueError('网格持仓尚未核对完成')
        pair = (self.variational_trader, self.lighter_trader)
        if any(self.position_traders(p) != pair for p in active):
            raise ValueError('网格本轮平台组合不一致，禁止加仓')
        if self.reconcile_open_positions(min_interval_seconds=0) != 'ok':
            raise ValueError('网格合计持仓核对失败，禁止加仓')
        return {(id(trader), symbol): sum(getattr(p, leg + '_size') *
                    (1 if self._snapshot_side(getattr(p, leg + '_side')) == 'long' else -1)
                    for p in active)
                for leg, trader in (('variational', self.variational_trader), ('lighter', self.lighter_trader))}

    def _grid_attach(self, position, context, direction):
        if context:
            position.grid_cycle_id = context['cycle_id']
            position.grid_layer = context['layer']
            position.grid_anchor_bps = context['anchor_bps']
            position.grid_direction = direction

    def _grid_filled(self, position):
        if not position.grid_cycle_id:
            return
        notional = max(position.entry_price_variational * position.entry_size_variational,
                       position.entry_price_lighter * position.entry_size_lighter)
        net = self._actual_entry_spread_bps(position) - position.entry_fees_usdc / notional * 10000
        if position.entry_target:
            if position.target_entry_net_bps is None:
                raise ValueError('网格实际净回归空间缺失')
            net = position.target_entry_net_bps
        position.grid_entry_net_bps = net
        if position.grid_anchor_bps is None:
            position.grid_anchor_bps = net
        self.history.append('grid_layer_opened', symbol=position.symbol, position_id=position.id,
                            grid_cycle_id=position.grid_cycle_id, grid_layer=position.grid_layer + 1,
                            grid_entry_net_bps=net, grid_anchor_bps=position.grid_anchor_bps)

    def _allocate_layer_funding(self, settlement, position, leg):
        return layer_funding(settlement, position, self.positions, leg)

    def _grid_exit_estimate(self, position):
        return self._executable_exit_estimate(position)

    @for_position
    def _executable_exit_estimate(self, position, funding_by_leg=None, *, record_stages=True):
        """Executable quotes, closing fees/movement and settled layer funding."""
        quantities = (position.variational_size, position.lighter_size)
        if min(quantities) <= 0 or abs(quantities[0] - quantities[1]) > 1e-8:
            return None
        quotes = (self._quotes(position.symbol, quantities[0]) if record_stages else
                  self._quotes(position.symbol, quantities[0], record_stages=False))
        pnl, funding_total, closing_notional = -position.entry_fees_usdc, 0.0, 0.0
        reference_pnl, closing_fee, reference_fee = pnl, 0.0, 0.0
        legs = {}
        for leg, trader, quote in zip(('variational', 'lighter'),
                (self.variational_trader, self.lighter_trader), quotes):
            qty, side = getattr(position, leg + '_size'), getattr(position, leg + '_side')
            is_long = self._snapshot_side(side) == 'long'
            fresh_quote(quote, position.symbol, qty)
            if isinstance(quote.get('fee_bps'), bool):
                return None
            fee = float(quote['fee_bps'])
            movement = self.execution_movement_bps(position.symbol, position.grid_direction)
            if funding_by_leg is not None:
                impacts = self._venue_slippage_bps(trader, position.symbol, qty, quote)
                impact = impacts[1 if is_long else 0]
            elif leg == 'lighter':
                book = quote.get('sizing_book') or trader.get_sizing_book(position.symbol)
                impact = self._book_slippage_bps(book,
                                                'bids' if is_long else 'asks', qty)
            else:
                impacts = self._venue_slippage_bps(trader, position.symbol, qty, quote)
                impact = impacts[1 if is_long else 0]
            if impact is None or not 0 <= fee < 10000 or impact + movement > self.slippage_tolerance:
                return None
            price = float(quote['bid' if is_long else 'ask']) * (1 + (-1 if is_long else 1) * (impact + movement) / 10000)
            legs[leg] = {'quote': dict(quote), 'side': 'sell' if is_long else 'buy',
                         'reference_price': float(quote['bid' if is_long else 'ask']),
                         'slippage_bps': impact + movement, 'bound': price,
                         'fee_bps': fee, 'movement_bps': movement, 'impact_bps': impact,
                         'liquidity_weight': self.close_liquidity.weight(
                             self._venue_name(trader, leg), position.symbol,
                             'sell' if is_long else 'buy', quote, qty)}
            pnl += (price - getattr(position, 'entry_price_' + leg)) * qty * (1 if is_long else -1)
            pnl -= price * qty * fee / 10000
            ref_price = legs[leg]['reference_price']
            reference_pnl += (ref_price - getattr(position, 'entry_price_' + leg)) * qty * (1 if is_long else -1)
            reference_pnl -= ref_price * qty * fee / 10000
            closing_fee += price * qty * fee / 10000
            reference_fee += ref_price * qty * fee / 10000
            closing_notional += price * qty
            funding = 0.0
            if funding_by_leg is not None:
                funding = funding_by_leg[leg]
            elif hasattr(trader, 'reconcile_funding'):
                settlement = self._allocate_layer_funding(
                    trader.reconcile_funding(position.symbol, position.open_time, datetime.now()), position, leg)
                if not settlement or settlement.get('complete') is not True:
                    return None
                funding = float(settlement['funding_usdc'])
            elif getattr(trader, 'venue', None) != 'arcus':
                return None
            funding_total += funding
        original = {name: leg['bound'] for name, leg in legs.items()}
        allocate_close_movement(legs, self.slippage_tolerance)
        for name, leg in legs.items():
            delta = leg['bound'] - original[name]
            qty = getattr(position, name + '_size')
            fee_delta = delta * qty * leg['fee_bps'] / 10000
            pnl += delta * qty * (1 if leg['side'] == 'sell' else -1) - fee_delta
            closing_fee += fee_delta
            closing_notional += delta * qty
        pnl += funding_total
        reference_pnl += funding_total
        if not math.isfinite(pnl):
            return None
        deadline = time.monotonic() + self.hedge_policy.max_delay_seconds
        for leg in legs.values():
            leg['quote']['submit_deadline_monotonic'] = deadline
        return {'net_pnl_usdc': pnl, 'net_pnl_bps': pnl / max(closing_notional, 1e-12) * 10000,
                'reference_net_pnl_usdc': reference_pnl, 'movement_cost_usdc': reference_pnl - pnl,
                'closing_fee_usdc': closing_fee, 'reference_closing_fee_usdc': reference_fee,
                'closing_fee_source': 'verified_account_rate_estimate', 'observed_at': utc_now(),
                'funding_usdc': funding_total, 'estimated': True, 'legs': legs}

    def _grid_profit_ok(self, estimate):
        return (estimate is not None and
                estimate['net_pnl_usdc'] >= self.close_min_profit_usdc + self.close_cost_reserve_usdc and
                estimate['net_pnl_bps'] >= self.close_min_profit_bps)

    def _grid_close_reason(self, position):
        monitor = getattr(self, 'position_monitor', None)
        estimate = monitor.grid_estimate(position) if monitor else self._grid_exit_estimate(position)
        if estimate is None:
            return None
        pnl = estimate['net_pnl_usdc']
        if self.max_loss_per_trade is not None and pnl <= -self.max_loss_per_trade:
            return '网格单层亏损保护'
        if self._grid_profit_ok(estimate):
            if position.entry_target:
                if self.close_mode == 'disabled':
                    return None
                if self.close_mode in ('convergence', 'convergence_and_profit'):
                    quotes = [estimate['legs'][name]['quote'] for name in ('variational', 'lighter')]
                    if not self._target_convergence(position, quotes)[0]:
                        return None
            return '网格逐层净收益退出'
        return None

    def grid_status(self):
        active = self._grid_active()
        grid = [p for p in active if p.grid_cycle_id]
        return {'mode': self.grid_policy.mode, 'active_layers': len(grid),
                'max_layers': self.grid_policy.max_layers, 'step_bps': self.grid_policy.step_bps,
                'layer_notional_usdc': self.grid_policy.layer_notional_usdc,
                'total_notional_usdc': self.grid_policy.total_notional_usdc,
                'used_notional_usdc': sum(max(p.variational_size * p.entry_price_variational,
                                               p.lighter_size * p.entry_price_lighter) for p in grid)}
