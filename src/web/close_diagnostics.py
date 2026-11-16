"""Display decision-time evidence separately from platform fill history."""
from src.execution.close_diagnostics import read_close_decisions
from src.web.rollback_explanation import display, VENUE_NAMES
from src.web.close_outcome import read_close_events, close_outcome


def explanation(row, decisions):
    if not decisions:
        return ('未保存平仓决策快照；旧版本记录或诊断证据暂缺，不能用平台成交价倒推当时行情。'
                if row.get('status') == 'closed' else '尚无已记录的平仓评估；触发后在此显示决策与发送前报价。')
    latest = decisions[0]
    trigger, check = latest.get('trigger') or {}, latest.get('pre_submit') or {}
    lines = ['平仓决策 ' + str(latest.get('decision_id', '未知'))]
    if check.get('protective_exit'):
        lines.append('风险保护退出，允许亏损平仓；持仓时长 '
                     + display(check.get('held_seconds'), ' 秒') + '；最长持仓设置 '
                     + display(check.get('max_hold_seconds'), ' 秒'))
    if trigger:
        target = {'center': '历史中心', 'zero': '零价差', 'center_or_zero': '历史中心或零价差',
                  'frozen_center': '本轮冻结历史中心', 'frozen_zero': '本轮零价差目标',
                  'zero_fallback_missing_center': '历史中心缺失，回退零价差',
                  'reduction': '相对入场收敛量'}.get(trigger.get('effective_target'), '未知')
        lines.append('触发评估时间：' + str(trigger.get('observed_at', '未知')))
        lines.append('触发原因：' + str(trigger.get('reason', '未知')) + '；实际目标：' + target)
        if trigger.get('target_bps') is not None:
            lines.append('本轮冻结目标：' + display(trigger['target_bps'], ' bps')
                         + '；冻结时间 ' + str(trigger.get('target_frozen_at') or '未知'))
        lines.append('当时方向价差 ' + display(trigger.get('directional_spread_bps'), ' bps')
                     + '；历史中心 ' + display(trigger.get('center_bps'), ' bps')
                     + '；距中心 ' + display(trigger.get('distance_to_center_bps'), ' bps')
                     + '；容差 ' + display(trigger.get('tolerance_bps'), ' bps'))
        if trigger.get('basis') != 'remaining':
            lines.append('相对入场收敛量：' + display(trigger.get('reduction_bps'), ' bps'))
        if trigger.get('target_mode') in ('center', 'either') and trigger.get('basis') == 'remaining':
            window = trigger.get('center_window_seconds', 604800)
            lines.append('中心统计窗口：' + display(window / 86400, ' 天') + '；样本 ' + display(trigger.get('center_samples')))
        for leg in ('variational', 'lighter'):
            quote = (trigger.get('legs') or {}).get(leg, {})
            name = VENUE_NAMES.get(quote.get('venue'), quote.get('venue') or leg)
            lines.append(name + ' 触发盘口 Bid / Ask ' + display(quote.get('bid')) + ' / '
                         + display(quote.get('ask')) + '；中间价 ' + display(quote.get('mid'))
                         + '；报价时间 ' + str(quote.get('updated_at') or '未提供')
                         + '；来源 ' + str(quote.get('source') or '未提供'))
    else:
        lines.append('触发快照缺失（直接调用或未经过收敛判断）；不补造触发行情。')
    lines.append('发送前复核时间：' + str(check.get('observed_at', '未知')))
    lines.append('复核结果：' + ('允许尝试提交（不代表成交）' if check.get('allowed') else '暂缓提交')
                 + '；' + str(latest.get('reason') or '原因未提供'))
    lines.append('保护限价下预计净收益 ' + display(check.get('net_pnl_usdc'), ' USDC')
                 + '；最低净收益 ' + display(check.get('minimum_usdc'), ' USDC')
                 + ' / ' + display(check.get('minimum_bps'), ' bps'))
    for leg in ('variational', 'lighter'):
        plan = (check.get('legs') or {}).get(leg, {})
        if not plan:
            continue
        name = VENUE_NAMES.get(plan.get('venue'), plan.get('venue') or leg)
        lines.append(name + ' ' + str(plan.get('close_side', '未知')) + ' ' + display(plan.get('size'))
                     + '；参考价 ' + display(plan.get('reference_price')) + '；保护限价 '
                     + display(plan.get('bound')) + '；报价时间 ' + str(plan.get('updated_at') or '未提供'))
        if plan.get('received_at') or plan.get('time_basis'):
            lines.append('平台时间 ' + str(plan.get('exchange_at') or '未提供')
                         + '；本机收到 ' + str(plan.get('received_at') or '未提供')
                         + ('；仅有接收时间，无法证明平台行情生成时效' if plan.get('time_basis') == 'receipt_only' else '')
                         + ('；沿用上次平台时间，未按本次接收时间刷新时效' if plan.get('time_basis') == 'exchange_last_observed' else '')
                         + '；来源 ' + str(plan.get('source') or '未提供'))
    if row.get('status') == 'closed':
        lines.append('实际平仓成交均价：' + display(row.get('close_price_variational')) + ' / '
                     + display(row.get('close_price_lighter')) + '；成交价差 '
                     + display(row.get('close_spread_bps'), ' bps') + '；最终净盈亏 '
                     + display(row.get('realized_pnl_usdc'), ' USDC'))
    lines.append('触发报价、发送前估算、实际成交是三个时刻；盘口时间不等于成交时间。诊断异步保存，异常退出或队列拥堵时可能缺失。')
    return '\n'.join(lines)


def attach_close_diagnostics(overview, data_dir):
    rows = [row for key in ('open_positions', 'closed_positions') for row in overview.get(key, [])
            if row.get('id') and row.get('hedge_status') != 'single_leg_recovered']
    snapshots = read_close_decisions(data_dir / 'close_diagnostics.sqlite', [row['id'] for row in rows])
    events = read_close_events(data_dir / 'hedge_events.sqlite', [row['id'] for row in rows])
    for row in rows:
        decisions = snapshots.get(row['id'], [])
        outcome = close_outcome(row, decisions, events.get(row['id'], []))
        text = explanation(row, decisions)
        if outcome['text']:
            text += '\n' + outcome['text']
        row['close_diagnostics'] = dict(status='available' if decisions else 'missing',
            decisions=decisions, outcome=outcome, text=text)
