# 执行代码交接 · 2026-09-27

行情服务统一运行 `./start.sh`；可用 `./start.sh status|logs|stop|restart` 管理服务。启动命令只启动行情与只读 API，不会自动开启实盘或提交订单。当前没有真实订单，也不把离线回执视为实盘验收。Lighter 主网、Lighter RH 和 StandX 的真实路径都保持默认关闭，必须由用户在面板完成只读鉴权后逐次开启。

## 已实现的路径

| 代码 | 实现 | 当前验收范围 |
|---|---|---|
| `src/lighter/execution.py` | 官方 SDK 限价 IOC，保护价格、整数数量/价格、只减仓、活动/历史订单与成交分页、定向取消、按持久意图恢复查询 | 发布 SDK 1.1.4 参数核对、受控传输测试；未签名真实账户 |
| `src/lighter/deployment.py` / `src/execution/platform_clients.py` | 主网与 Robinhood Chain 独立 REST、WebSocket 和 L2 签名域 | RH 使用独立账户和 API key；不会复用主网凭证 |
| `src/standx/client.py` / `src/standx/execution.py` | BSC 钱包登录、临时 Ed25519 请求签名、IOC 订单、订单/成交/费用查询、只减仓 | 需用户钱包只读鉴权；Solana 暂未接入 |
| `src/variational/execution.py` | 官网精确控件、基础币单位/数量/方向/只减仓检查、RFQ 绑定、单次点击、回执验证 | 公开页面 DOM 已核对，登录态提交文案和确认框未验证 |
| `src/variational/receipt_reader.py` | Selenium/CDP 只读观察网站请求；按账户、报价编号、市场、数量筛选响应 | 需要当前账户响应字段映射；未虚构字段或订单终态 |
| `src/execution/journal.py` | SQLite 提交前持久化、相同编号防重复、未知订单阻止新提交、进程锁 | 离线测试，数据库保留恢复依据 |
| `src/execution/worker.py` / `runtime.py` | 单进程执行、启动条件检查、账户/报价检查、停止开关、资源清理 | 默认关闭；无浏览器/签名账户联调 |

## 可以直接运行的检查

```bash
.venv/bin/python -B scripts/rehearse_execution.py
.venv/bin/python -B scripts/execution_cli.py
```

第一条是合成回执的离线演练，包含部分成交、超时、拒绝重复提交、只减仓和锁。第二条仅报告缺失条件，不连接账户或启动浏览器。

独立执行依赖已安装在 `.venv-execution`。迁移电脑时运行 `bash setup_execution.sh`，脚本使用本机 7890 代理。官方 SDK/Selenium 不加入日常行情 `.venv`，避免旧浏览器依赖影响监控。

## 登录后仍须落实的配置

参考 `config/variational-execution.example.json`，账户验收后另存本机配置。布尔标志只表达已完成验收，不能靠改成 true 替代检查。

- 实际账户标识、BTC/ETH 基础币数量精度、登录后的精确提交文案。
- 同一账户、同一基础币数量的可执行 RFQ。只读扩展转发的公开/indicative 参考数据不能自动升级为执行授权。
- 网站自己请求的账户、报价、订单历史响应路径及 JSON 点分字段映射。字段必须来自实际响应：账户、quote ID、side、requested size、filled size、average price、order ID、终态、实际费用、资金费。
- 已确认终态的枚举映射。如果真实页面需要额外确认框，先依据该登录页补专用处理；当前遇到未识别流程会停止，不盲点。
- 账户余额与持仓读取要求完整、时间新鲜；如果官网结构无法通过点分路径表示，还需依据真实样本补明确转换，不能伪造规范字段。
- Lighter 本地 `LIGHTER_API_PRIVATE_KEY`、`LIGHTER_ACCOUNT_INDEX`、`LIGHTER_API_KEY_INDEX`。当前运行入口只允许已核验零交易费的账户；实际非零费用字段的单位尚未验收，保留未知并停止继续开仓。

浏览器执行仅连接用户独立启动、仅绑定本机的已登录调试会话。代码不自动登录、保存钱包秘密、弹出或确认钱包签名。浏览器数量与只减仓控件已依据官网的 `data-testid` 核对，选择器失配即停止。

**实盘使用入口**：打开“设置 → 密钥与浏览器连接”，完成平台只读验证后，在首页选择恰好两个平台。只读检查通过后才会短时解锁实盘开关；保存密钥、刷新页面或服务重启都不会自动下单。Variational 仍要求独立已登录浏览器会话；Lighter RH 与 StandX 不会弹出交易网页。

## 核对与恢复

- 成交以终态回执为依据。HTTP 成功、交易哈希、按钮点击成功、订单已接受都不是完全成交。
- SDK 由其单密钥 nonce manager 分配 nonce；超时不重新提交，只查询同一 client order index。
- `data/execution_orders.sqlite` 保存本地计划和净化后的回执，不保存密钥、认证 token 或原始请求。未知订单会保留，`LighterExecutionAdapter.reconcile_order(sequence)` 可只读查询；不会重新发单。
- Lighter 非零交易费单位和本次平仓资金费未验证时返回 `None`。协调器不按零入账；已确认的平仓继续减掉另一条已知腿，随后锁住新开仓等待账务核对。
- 停机文件 `data/STOP_EXECUTION` 或 Ctrl+C 阻止后续执行；不自动宣称平仓。重启发现未完成策略状态仍需人工核对，当前没有自动重建全部真实账户持仓的功能。
- 当前执行仅考虑一个已验收 BTC/ETH 永续市场；不对 US100 Swap / perp 自动交易。

依据：官方 [Lighter SDK](https://github.com/elliottech/lighter-python)、[Omni 费用](https://docs.variational.io/omni/trading/fees)、[Omni 滑点](https://docs.variational.io/omni/trading/slippage)。


## 单次上限、缩量和本次账户检查

- `strategy.trading.position_size` 现在表示单次每腿名义金额上限（非保证金），本机已按最新授权设置 12 USDC。
- 固定亏损阈值 `max_loss_per_trade` / `max_daily_loss` 可设 `null` 关闭。本机按用户要求关闭；残余敞口、未知回执、重复订单、持仓数与频次保护保留。多空双开仍可能产生亏损。
- 缩量器按买入 Ask / 卖出 Bid 逐档检查最低净价差、已核验手续费、滑点预算和保护价名义金额。Lighter 执行限价覆盖最后一档所需深度，不用 VWAP 均价代替盘口末档；最终净价差按该保守价格复核。两腿基础币数量一致；深度不足会缩量，数量改变必须请求对应新 RFQ，最多六轮，持续变动则跳过。当前 Lighter 适配器要求双边深度都覆盖数量，因此会保守限制数量。
- 页面只有参考缩量预览：Lighter 账户费率和 V 精度/RFQ 未验收，不能当作净利润或成交承诺。最终盈亏还包括平仓价格、手续费和资金费。
- 2026-09-27 只读官方接口核对：Lighter BTC 最小名义金额 10 USDC，最小数量 0.00007 BTC；1 USDC 上限会跳过，不自动提高上限。限制随平台变化，以运行时检查为准。
- Chrome 本次实际测试了 V 的 1 USDC 金额和买卖方向切换，页面显示报价、滑点和 0 可交易余额；提交位置仍显示“验证”，账户登录未验收。未提交订单、未签署交易，不能把表单测试当作实盘验证。
- Lighter 页面核对账户索引 750057，API key 索引 4；页面第二串是公钥。聊天中出现的私钥应撤销/替换。项目没有保存聊天密钥。

更换密钥后在本地终端运行（输入不回显，检查成功才保存；不下单）：

```bash
.venv-execution/bin/python -B scripts/check_lighter_account.py --save
```

输入新 API key 索引与新私钥；脚本将校验官方 SDK 签名身份与鉴权只读查询。默认账户索引 750057，可用 `--account-index` 指定其他已授权账户。该检查通过仍需完成 V 账户、数量精度、两边费率和回执验收。

执行尝试历史在 `data/hedge_events.sqlite`，包括计划、两腿回执、平仓回执和阻断原因；订单意图仍保存在 `data/execution_orders.sqlite`。面板“每次执行历史”读取最近 100 条，不生成虚构成交。历史记录不能替代止损或敞口保护。


### 入金后 ETH 只读检查

最新只读检查快照见 `data/eth_preflight.json`：V 已显示登录账户、可交易 14.90 USDC、无仓位；Lighter 账户 750057 可用 24.9943 USDC、无持仓。ETH 与 BTC 的 Lighter 最小名义金额都为 10 USDC，ETH 最小数量 0.002、步长 0.0001。此前每腿 1 USDC 不满足要求；最新授权已调整为每腿 12 USDC，仍须逐次检查最小金额。

约 0.004 ETH / 腿（当时约 10.79 USDC / 腿）只是提交前候选数量，需核对 V 精度、最小订单、对应数量 RFQ 和成交回执。本次没有提交任何订单。旧的未入金/未登录界面观察已被这次余额与界面观察更新，但执行适配器验收标志仍保持关闭。

Lighter 本地尚未配置有效私钥。更换已在聊天中暴露的密钥后，通过本地输入执行 ETH 鉴权只读检查：

```bash
.venv-execution/bin/python -B scripts/check_lighter_account.py --symbol ETH --save
```


### 12 USDC 表单演示与本机密钥入口（最新）

已在用户登录的原生 Chrome 中，通过 CUA 切换 ETH 数量单位、填入 0.004 ETH、分别选择买与卖；页面显示约 10.82 USDC、费用 0%、可交易余额 14.90 USDC，两个方向提交按钮均可用。未点击提交，未发单；这不是项目 Selenium 执行路径或双腿成交验收。页面最大滑点 0.5%，策略上限 0.1%，仍需在运行前一致性核验。

仪表盘展开“密钥与浏览器连接”，填写账户索引、API key 索引、新 API 私钥，点击“验证并保存到本机”。密钥经本机端点传入独立官方 SDK 只读查询进程，通过后原子写入权限 600 的 `.env`。失败不覆盖原文件；不返回密钥、不存 localStorage、不启用交易。页面刷新或服务重启后，未重新鉴权的配置只显示“已保存”。此前聊天密钥不复用。新启动的执行进程读取该文件；已经运行的进程不能在线换钥。

Variational 不会自动接管任意日常 Chrome。`browser_debugger_address` 必须配置为独立浏览器会话的 `127.0.0.1:端口`。执行代码仅在该会话内查找唯一、完整匹配的官网市场标签页；零个或多个匹配即停止并恢复原标签页，不扫描端口、不跳转、不自动登录。真实 RFQ、账户和回执映射仍须验收，示例执行开关保持关闭。

本次回归：84 项 Python 测试、前端百分比测试、报价桥接测试通过；其中受控执行演练 18 项通过。新增覆盖密钥保存权限、不泄露、拒绝跨站/DNS 重绑定请求、失败保留旧文件、浏览器重复标签与仿冒域名阻断。没有真实订单。


最新增量：单次 ETH 执行入口、只读浏览器字段采集命令及操作说明见 RUN_ETH_TEST.md。单次入口不循环、不管理旧仓位；验收条件未满足时不发单。页面优先显示双腿毛价差金额、手续费和扣费后入场价差，V 单平台点差移至展开说明，点差/深度不重复扣除，未知费率不当成零。Lighter 鉴权现有分阶段诊断、误填公钥识别与只读复查；通过本机 7890 的官方签名器只读诊断已确认能收到服务器的密钥不匹配响应（使用临时未注册测试密钥，未使用用户私钥，未下单）。当前用户密钥仍未鉴权保存，真实 Variational RFQ/回执仍未验收。97 项离线测试通过。
