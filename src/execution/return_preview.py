"""Pure, shared round-trip estimate. No account reads, requests or orders."""
from src.execution.target_return import directed_spread, exit_projection, projected_return, finite
from src.execution.funding_forecast import funding_scenario


def choose_target(mode, center, samples, current):
    if mode not in ('center', 'zero', 'either'):
        raise ValueError('回归目标无效')
    if mode != 'zero' and (center is None or int(samples or 0) < 30):
        raise ValueError('历史中心样本不足30个，等待；不会自动改用零目标')
    targets = [('zero', 0.0)] if mode == 'zero' else [('center', finite(center, '历史中心'))]
    if mode == 'either':
        targets.append(('zero', 0.0))
    favorable = [item for item in targets if item[1] < current]
    return max(favorable or targets, key=lambda item: item[1])


def round_trip_preview(buy, sell, quantity, target_mode, center, samples, policy, target_override=None):
    """Use four actual book impacts and two future closing movement budgets.

    Entry reserve covers opening price uncertainty in the profit floor.
    Opening movement remains an order-price guard, not a second profit debit.

    A missing side/fee/impact remains unknown. This estimate never establishes
    executable eligibility; the coordinator still verifies size/account/price.
    All bps use the larger opening leg, exactly as the execution model does.
    """
    result = dict(version=1, cost_model_version=2, requested_target_mode=target_mode, target_mode=None,
                  target_bps=None, net_bps=None, profit_usdc=None, minimum_bps=None,
                  gap_bps=None, meets_floor=False, costs={}, reason=None)
    try:
        qty = finite(quantity, '估算同数量', 1e-100)
        reference = max(finite(buy['ask'], '买价', 1e-100), finite(sell['bid'], '卖价', 1e-100))
        gross = directed_spread(buy['ask'], sell['bid'])
        mode, target = (('channel', finite(target_override, '通道冻结目标')) if target_override is not None
                        else choose_target(target_mode, center, samples, gross))
        result.update(target_mode=mode, target_bps=target, quantity=qty, reference_notional=qty*reference)
        movement = finite(policy.get('movement_bps'), '每腿价格变动预算', 0)
        funding = funding_scenario(buy, sell, policy)
        funding_budget = finite(funding['budget_bps'], '整轮资金费预算', 0)
        impacts = [finite(q.get(side + '_impact_bps'), label, 0) for q, side, label in (
            (buy, 'buy', '开多盘口冲击'), (sell, 'sell', '开空盘口冲击'),
            (buy, 'sell', '平多盘口冲击'), (sell, 'buy', '平空盘口冲击'))]
        slips = [value + movement for value in impacts]
        ceiling = finite(policy.get('slippage_tolerance'), '每腿滑点上限', 0)
        projection = exit_projection(buy, sell, target, slips[2], slips[3],
            funding_budget,
            max(float(buy['ask']), float(sell['ask'])) * (1+max(impacts[:2])/10000))
        edge = projected_return(buy['ask'], sell['bid'], qty, buy.get('fee_bps'), sell.get('fee_bps'),
                                impacts[0], impacts[1], projection)
        closing_notional = qty * (projection['close_sell_price'] + projection['close_buy_price'])
        amount_floor = max(finite(policy.get('min_profit_usdc', 0), '最低平仓收益', 0)
                           + finite(policy.get('cost_reserve_usdc', 0), '平仓成本预留', 0),
                           finite(policy.get('min_profit_bps', 0), '最低平仓收益bps', 0)*closing_notional/10000)
        floor = max(finite(policy.get('base_min_bps'), '最低入场门槛', 0),
                    amount_floor/(qty*reference)*10000) + finite(policy.get('entry_reserve_bps'), '入场预留', 0)
        net = edge['net_edge_bps']
        upper = finite(policy.get('max_bps', 100000), '收益上限', 0)
        reason = (f'整轮净收益 {net:.2f} bps，距入场门槛还差 {floor-net:.2f} bps' if net < floor
                  else '整轮净收益超过策略上限' if net > upper
                  else '盘口冲击加价格预算超过每腿上限' if max(slips) > ceiling
                  else '估算达到收益门槛；数量、账户及最新报价待核验')
        # Allocate every debit once. The exit spread includes the selected
        # directed target AND both future quote widths (before close budgets).
        raw_projection = exit_projection(buy, sell, target, 0, 0, 0)
        costs = dict(open_fees_bps=edge['opening_fees_usdc']/(qty*reference)*10000,
                     open_impact_and_movement_bps=gross-edge['opening_edge_bps']-edge['opening_fees_usdc']/(qty*reference)*10000,
                     target_and_close_width_bps=(raw_projection['close_buy_price']-raw_projection['close_sell_price'])/reference*10000,
                     close_impact_and_movement_bps=((projection['close_buy_price']-projection['close_sell_price'])
                         -(raw_projection['close_buy_price']-raw_projection['close_sell_price']))/reference*10000,
                     close_fees_bps=projection['close_fees_per_unit']/reference*10000,
                     funding_bps=projection['funding_cost_per_unit']/reference*10000)
        # Keep the legacy additive costs for API compatibility, while the UI
        # separates remaining target gap, actual book costs and risk budgets.
        target_gap = (raw_projection['target_sell_bid'] - raw_projection['target_buy_ask']) / reference * 10000
        expected_funding = funding.get('expected_cost_bps')
        known_funding = expected_funding if expected_funding is not None else funding_budget
        base_projection = exit_projection(buy, sell, target, impacts[2], impacts[3], known_funding,
            max(float(buy['ask']), float(sell['ask'])) * (1+max(impacts[:2])/10000))
        base_edge = projected_return(buy['ask'], sell['bid'], qty, buy.get('fee_bps'), sell.get('fee_bps'),
                                     impacts[0], impacts[1], base_projection)
        book_open = gross-base_edge['opening_edge_bps']-base_edge['opening_fees_usdc']/(qty*reference)*10000
        book_close = ((base_projection['close_buy_price']-base_projection['close_sell_price'])
                      -(raw_projection['close_buy_price']-raw_projection['close_sell_price']))/reference*10000
        fee_total = costs['open_fees_bps'] + costs['close_fees_bps']
        components = dict(target_remaining_bps=target_gap,
            close_quote_width_bps=costs['target_and_close_width_bps']-target_gap,
            fees_bps=fee_total, book_impact_bps=book_open+book_close,
            movement_budget_bps=costs['open_impact_and_movement_bps']+costs['close_impact_and_movement_bps']-book_open-book_close,
            funding_cost_bps=costs['funding_bps'] if expected_funding is None else costs['funding_bps']*known_funding/funding_budget if funding_budget else 0,
            funding_extra_bps=0 if expected_funding is None else costs['funding_bps']- (costs['funding_bps']*known_funding/funding_budget if funding_budget else 0))
        result.update(net_bps=net, profit_usdc=edge['estimated_entry_edge_usdc'], minimum_bps=floor,
                      gap_bps=max(0.0, floor-net), meets_floor=0 < net and floor <= net <= upper and max(slips) <= ceiling,
                      opening_net_bps=edge['opening_edge_bps'], gross_bps=gross, costs=costs,
                      entry_reserve_bps=float(policy['entry_reserve_bps']),
                      opening_movement_cost_bps=0.0,
                      movement_bps=movement, movement_evidence=policy.get('movement_evidence'),
                      components=components, funding=funding,
                      expected_net_bps=base_edge['net_edge_bps'] if expected_funding is not None else None,
                      before_risk_budget_bps=gross-sum(v for k,v in components.items()
                          if k not in ('movement_budget_bps','funding_extra_bps')),
                      reason=reason)
    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
        result['reason'] = str(exc)
    return result


def preview_policy(settings):
    risk, entry, close = (settings.get(k, {}) or {} for k in ('risk', 'entry_policy', 'close_policy'))
    spread = settings.get('spread_threshold', {}) or {}
    return dict(movement_bps=risk.get('execution_price_movement_bps', 2),
                slippage_tolerance=risk.get('slippage_tolerance', 10),
                funding_reserve_bps=entry.get('funding_reserve_bps'),
                funding_mode=entry.get('funding_mode', 'fixed'),
                funding_hold_seconds=entry.get('funding_hold_seconds', 3600),
                funding_extra_bps=entry.get('funding_extra_bps', 1),
                base_min_bps=spread.get('min', 5), max_bps=spread.get('max', 50),
                entry_reserve_bps=risk.get('entry_extra_profit_bps', risk.get('execution_safety_margin_bps', 0)),
                min_profit_usdc=close.get('min_profit_usdc', 0), min_profit_bps=close.get('min_profit_bps', 0),
                cost_reserve_usdc=close.get('cost_reserve_usdc', 0))
