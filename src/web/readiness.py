"""Explain evidence and missing integration work without authorizing execution."""
def readiness_report(verified, saved, bridge, acceptance, profile=None):
    from src.variational.omni_contract import zero_trade_fee_policy
    zero_fee=zero_trade_fee_policy((profile or {}).get("response_schema",{}).get("fee_policy"))
    return [
        {'title': 'Lighter 密钥与账户费率', 'status': 'passed' if verified else 'pending',
         'evidence': '当前只读鉴权和费率查询通过。' if verified else ('密钥已保存在本机；本次鉴权未完成或已过期。' if saved else '本机尚未保存 API 密钥。'),
         'action': '无需重填密钥；执行前仍需验证订单权限和实际成交回执。' if verified else '设置 → 密钥与浏览器连接 → 只读复查已保存密钥。'},
        {'title': 'Variational 实时报价', 'status': 'passed' if bridge['symbols'] else 'pending',
         'evidence': '10 秒内收到 '+ ' / '.join(bridge['symbols'])+' 数量报价。' if bridge['symbols'] else '10 秒内没有收到数量报价；缓存价格不作为实时入场信号。',
         'action': '保持官网同一市场页面打开并填入数量；只读扩展持续同步，无需重复下单。'},
        {'title': 'Variational 手动成交链路', 'status': 'observed' if acceptance else 'pending',
         'evidence': '已核对买卖两笔 RFQ、订单、成交和历史空仓快照；自动执行尚未验证。' if acceptance else '尚无完整手动开平仓验收报告。',
         'action': '已有样本可以继续开发，不用再做一次手动开平仓。' if acceptance else '先采集脱敏订单与成交回执。'},
        {'title': 'Variational 自动执行会话与控件', 'status': 'passed' if (profile or {}).get('form_preparation_evidence',{}).get('ready') else 'pending',
         'evidence': ('程序会话已验证 ETH 基础币输入、买卖切换、策略滑点、同数量报价及操作前后空仓；这项准备不提交订单。' if (profile or {}).get('form_preparation_evidence',{}).get('ready') else '账户及交易表单仍需核对；日常 Chrome 与程序调试 Chrome 可能是不同会话。'),
         'action': '点击“准备 V 执行会话（不下单）”可自动恢复空窗口并设置表单；所有下单前仍会重新校验。'},
        {'title': 'Variational 回执解析', 'status': 'passed' if (profile or {}).get('receipt_schema_verified') else 'pending',
         'evidence': '已用当前账户历史接口回放既有真实买入和只减仓平仓；核对账户、RFQ、数量、成交均价与终态。' if (profile or {}).get('receipt_schema_verified') else '回执解析尚未用真实记录回放核对。',
         'action': '回放不产生新交易，也不代表自动双腿实盘已验收；首次真实运行仍需核对新回执。'},
        {'title': 'Variational 普通交易手续费', 'status': 'passed' if zero_fee else 'pending',
         'evidence': '已核验官方普通交易零费政策；本次报价和成交回执仍必须返回零费用。' if zero_fee else '零费政策证据缺失或已超过 30 天。',
         'action': '保留政策与报价来源；非零费用、强平和其他交易类型不套用零费规则。'},
        {'title': 'Variational 资金费估算与核账', 'status': 'observed',
         'evidence': '已接入官网预计费用、数量规则和可见资金费；已实现 PNL 支持账户＋RFQ 关联。资金费入场按顶部费率与周期预测；资金费结算可后补；两腿成交及账户空仓确认后，不因仅缺资金费而阻止新开仓，最终净收益保持未知。',
         'action': '不要求先产生资金费记录；只要持仓跨结算时点，仍需核对实际扣收。未发生实际结算前不声明最终净收益。'},
        {'title': '双腿成交与异常恢复', 'status': 'pending',
         'evidence': '已实现执行协调与日志；实际双腿、部分成交、断线和重启后的持仓重建未验收。',
         'action': '先完成上面的适配与只读检查，再由操作者发起单次实盘。Lighter 最低金额和数量步长由检查接口实时获取，不自动加大上限。'},
    ]
