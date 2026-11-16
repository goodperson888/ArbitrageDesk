"""Entry spread accounting: execution prices already include quoted spread/depth."""
import math


def _bps(value, allow_negative=False):
    return (type(value) in (int, float) and math.isfinite(value) and
            (allow_negative or value >= 0))


def entry_breakdown(comparison, lighter_fee_bps=None, variational_fee_bps=0,
                    funding_bps_per_8h=None, *, funding_interval_s=28800,
                    variational_fee_source='manual_prediction', funding_source='manual_prediction'):
    c = comparison
    q = c.get('base_quantity')
    lighter_fee_known = _bps(lighter_fee_bps)
    variational_fee_known = _bps(variational_fee_bps)
    funding_known = _bps(funding_bps_per_8h, allow_negative=True)
    hours = funding_interval_s / 3600
    funding_rate_bps = funding_bps_per_8h * funding_interval_s / 28800 if funding_known else None
    result = {'reference_only': True, 'executable': False, 'directions': {},
              'variational_fee_bps': variational_fee_bps if variational_fee_known else None,
              'lighter_fee_bps': lighter_fee_bps if lighter_fee_known else None,
              'funding_bps_per_8h': funding_bps_per_8h if funding_known else None,
              'funding_rate_bps': funding_rate_bps,
              'funding_basis': f'仅 Variational 腿；正费率按多头支付、空头收取估算，单个 {hours:g} 小时周期；未含 Lighter 资金费' if funding_known else None,
              'variational_fee_source': variational_fee_source,
              'funding_source': funding_source, 'funding_interval_s': funding_interval_s,
              'funding_usdc': None, 'exit_cost_usdc': None, 'actual_slippage_usdc': None}
    unknown_reasons = []
    if not variational_fee_known:
        unknown_reasons.append('Variational 当前数量报价未提供可核验预计费用')
    if not lighter_fee_known:
        unknown_reasons.append('Lighter 费率证据缺失或已过期，请在设置中只读复查')
    if not funding_known:
        unknown_reasons.append('资金费仅在页面可见或结算后计入，当前未纳入净价差')
    result['unknown_reasons'] = unknown_reasons
    for name, buy, sell, topbuy, topsell, vp, lp in (
        ('a', c.get('variational_ask'), c.get('lighter_sell_vwap'), c.get('variational_ask'), c.get('lighter_bid'), c.get('variational_ask'), c.get('lighter_sell_vwap')),
        ('b', c.get('lighter_buy_vwap'), c.get('variational_bid'), c.get('lighter_ask'), c.get('variational_bid'), c.get('variational_bid'), c.get('lighter_buy_vwap'))):
        if not all(type(x) in (int, float) and math.isfinite(x) and x > 0 for x in (q,buy,sell,topbuy,topsell)):
            result['directions'][name] = {'available': False}
            continue
        gross = q*(sell-buy)
        variational_fees = q*vp*variational_fee_bps/10000 if variational_fee_known else None
        lighter_fees = q*lp*lighter_fee_bps/10000 if lighter_fee_known else None
        fees = (variational_fees or 0) + (lighter_fees or 0) if variational_fees is not None and lighter_fees is not None else None
        net = gross-fees if fees is not None else None
        funding = (q*vp*funding_rate_bps/10000 * (1 if name == 'a' else -1)
                   if funding_known else None)
        result['directions'][name] = {
            'available': True, 'quantity': q, 'buy_price': buy, 'sell_price': sell,
            'price_gap_usdc_per_unit': sell-buy,
            'entry_notional_usdc': q*buy,
            'gross_usdc': gross, 'gross_convergence_usdc': gross,
            'gross_bps': (sell/buy-1)*10000,
            'depth_impact_usdc': q*(topsell-topbuy)-gross,
            'variational_notional': q*vp, 'lighter_notional': q*lp,
            'variational_fees_usdc': variational_fees, 'lighter_fees_usdc': lighter_fees,
            'fees_usdc': fees, 'funding_estimate_usdc': funding,
            'funding_estimate_scope': 'Variational 单腿；未含 Lighter 资金费',
            'funding_estimate_basis': result['funding_basis'],
            'net_entry_usdc': net,
            'after_known_entry_fees_usdc': net,
            'net_entry_bps': net/(q*buy)*10000 if net is not None else None,
        }
    return result
