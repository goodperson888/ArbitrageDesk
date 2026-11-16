"""Read-only monitor sizing. Public/indicative quotes cannot authorize execution."""
from src.common.sizing import allocate


def allocation_preview(row, settings):
    cap = settings.get('trading', {}).get('position_size', 1)
    v = row.get('details', {}).get('variational', {})
    l = row.get('details', {}).get('lighter', {})
    result = {'cap_usdc': cap, 'executable': False, 'plans': [], 'status': '等待行情',
              'note': '按相同基础币数量配对；金额是每腿上限。参考估算未扣 Lighter 账户费用，不能认定净利润。'}
    minimum = l.get('min_quote_amount')
    result['lighter_min_quote_usdc'] = minimum
    if minimum is not None and cap < float(minimum):
        result['status'] = '低于 Lighter 最小下单金额 '+str(minimum)+' USDC，跳过'
        return result
    if row.get('instrument_mismatch'):
        result['status'] = '合约类型不同，禁止分配'
        return result
    if not v.get('bid') or not v.get('ask') or not row.get('book'):
        return result
    if l.get('size_decimals') is None:
        result['status'] = 'Lighter 数量精度未知'
        return result
    # Monitor has no verified Variational size precision or executable RFQ.
    limit = v.get('quote_size') or float(v.get('quote_notional_usdc') or cap) / max(v['bid'], v['ask'])
    protection_cap = settings.get('risk', {}).get('slippage_tolerance', 10)
    observed = row.get('comparison', {}).get('slippage', {}).get('directions', {})

    def observed_value(direction, leg):
        key = 'variational_bps' if leg == 'variational' else 'lighter_bps'
        value = observed.get(direction, {}).get(key)
        return value if type(value) in (int, float) and 0 <= value <= protection_cap else None

    legs = {
        'buy_variational': {'buy': observed_value('a', 'variational'),
                            'sell': observed_value('a', 'lighter')},
        'sell_variational': {'buy': observed_value('b', 'lighter'),
                             'sell': observed_value('b', 'variational')},
    }
    missing = {direction for direction, values in legs.items() if any(value is None for value in values.values())}
    if missing == set(legs):
        result['status'] = '等待两平台新鲜滑点观测；缺失时不使用固定回退'
        return result

    slippage = {
        direction: {side: (value if value is not None else protection_cap) for side, value in values.items()}
        for direction, values in legs.items()
    }
    plans = allocate({'bid':v['bid'], 'ask':v['ask'], 'fee_bps':0}, row['book'], cap,
                     l['size_decimals'], settings.get('spread_threshold', {}).get('min', 5),
                     settings.get('spread_threshold', {}).get('max', 50),
                     slippage, 0, max_quantity=limit)
    for plan in plans:
        direction = 'a' if plan['direction'] == 'buy_variational' else 'b'
        plan['variational_slippage_bps'] = legs[plan['direction']]['buy' if plan['direction'] == 'buy_variational' else 'sell']
        plan['lighter_slippage_bps'] = legs[plan['direction']]['sell' if plan['direction'] == 'buy_variational' else 'buy']
        plan['slippage_breakdown'] = (
            '买 V 页面预计滑点 + 卖 L 同数量盘口冲击'
            if plan['direction'] == 'buy_variational'
            else '买 L 同数量盘口冲击 + 卖 V 页面预计滑点')
        plan['slippage_mode'] = '实时观测优先；缺失或超过保护上限时不下单'
        plan['slippage_observed_legs'] = observed.get(direction, {}).get('known_legs', 0)
        if plan['direction'] in missing:
            plan.update(available=False, reason='该方向缺少新鲜平台滑点观测；不会套用固定回退')
        if plan['available'] and (plan['quantity'] < float(l.get('min_base_amount') or 0) or
                                 plan['lighter_notional'] < float(minimum or 0)):
            plan.update(available=False, reason='缩量后低于 Lighter 最小下单数量或金额')
    result.update(plans=plans, status='参考缩量；实际执行重新核对账户费率、数量精度和同数量报价')
    return result
