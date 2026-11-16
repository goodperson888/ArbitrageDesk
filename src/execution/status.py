"""Development status, intentionally separate from live account readiness."""


def implementation_status():
    return {
        'live_enabled': False,
        'components': [
            {'name': 'Lighter 官方 SDK 执行', 'implemented': '精度、IOC 保护价、签名提交、订单终态/部分成交查询、只减仓',
             'pending': '当前鉴权和费率见运行面板；非零费用单位、资金费与真实订单联调待验收'},
            {'name': 'Lighter RH / Robinhood Chain', 'implemented': '独立 REST origin、API key、签名域和同一 IOC/回执协调器',
             'pending': '在设置页完成 RH 账户只读鉴权和费率核验'},
            {'name': 'StandX Perps', 'implemented': 'BSC 钱包登录、Ed25519 请求体签名、IOC、订单/成交/费用查询、只减仓',
             'pending': '在设置页完成钱包鉴权和账户费率核验；Solana 暂未接入'},
            {'name': 'Variational 网页执行', 'implemented': '实际报价→RFQ→订单→成交解析已接入读取器；嵌套持仓、分页、账户指纹、滑点校验',
             'pending': '会话及历史回执验证见运行面板；新自动订单终态、非零费用及资金费结算仍待验收'},
            {'name': '执行协调与恢复', 'implemented': '逐档缩量、同币数量配对、每腿金额上限、数量绑定重新报价、执行历史、未知状态停机、进程锁',
             'pending': '整套账户重建与双腿实盘验收；不得把受控演练视为实盘验证'}],
        'rehearsal_command': '.venv/bin/python -B scripts/rehearse_execution.py',
        'note': '本机密钥入口仅做只读鉴权与保存；当前缺项以运行面板为准。单次执行由操作者发起，后台重新核对报价、账户及金额。'}
