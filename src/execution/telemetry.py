"""Fixed diagnostic messages only: never log raw SDK/browser exception text."""
import time
import math
import traceback
import re
from pathlib import Path
from contextlib import contextmanager

REASONS = frozenset([
 '通道历史统计正在积累或更新，暂不入场','通道没有有效波动宽度，暂不入场',
 '通道入场方向与回归方向不一致','最新双平台报价已离开通道入场边界',
 '该品种仍有未结束持仓，通道反向需先确认双边结束',
 '对侧循环等待反向边界；反向净收益仍须达标','同侧通道等待回到边界内后再次触边',
 '资金费快照待后台更新；本次不阻塞平仓发送','资金费快照正在后台更新；本次不等待网络读取',
 '没有符合净价差与金额上限的深度','重新报价后净价差不满足条件',
 '重新报价超过每腿上限','连续报价变化，跳过本次机会','上限低于数量精度',
 '缩量盘口过期','没有可配对的深度','报价非实时、非可执行或合约/数量不符',
 '买卖价无效','未收到该账户、该数量的可执行 RFQ','RFQ 编号缺失',
 '网页准备后 RFQ 已变化，需重新评估双腿价差','网页准备后 RFQ 已变化且价格越过保护范围，未提交','官网滑点上限宽于策略限制，未提交',
 '页面数量或名义金额与计划不符','页面方向与计划不符','页面只减仓状态与计划不符',
 '官网当前预计滑点缺失','官网当前预计滑点缺失，未提交',
 '官网当前预计滑点超过本次保护上限，未提交',
 '尚未识别官网当前预计滑点','官网预计滑点超出支持范围',
 '官网当前预计滑点缺失，等待新报价',
 '官网预计滑点超过保护上限，等待新报价',
 'Lighter 同数量盘口滑点缺失或超过保护上限，等待新报价',
 '净价差超过策略上限，等待下一轮报价'])


def rejection_quote(events, rejection):
    """Best direction in the last quote batch of this exact failed attempt.

    Never join a newer scan, another symbol, or an earlier successful batch
    onto a rejection. Events are supplied newest first by TradeHistory.
    """
    detail = (rejection or {}).get('detail') or {}
    execution_id, symbol = detail.get('execution_id'), detail.get('symbol')
    if not execution_id or not symbol:
        return None
    batch = []
    for event in events:
        if event.get('id', 0) > rejection.get('id', 0):
            continue
        d = event.get('detail') or {}
        if d.get('execution_id') != execution_id or d.get('symbol') != symbol:
            continue
        if event.get('event') == 'quote_evaluation':
            if batch and (d.get('quantity') != batch[0]['detail'].get('quantity') or
                          d.get('direction') == batch[0]['detail'].get('direction')):
                break
            edge = d.get('net_edge_bps')
            if type(edge) in (int, float) and math.isfinite(edge):
                batch.append(event)
        elif batch:
            break
    return max(batch, key=lambda event: event['detail']['net_edge_bps'], default=None)

SAFE_MARKET_REASONS = frozenset([
    '持仓报价过期，平仓条件暂无法判断', '持仓盘口无效，平仓条件暂无法判断',
    '持仓账户待核验', '持仓行情待更新，平仓条件暂无法判断',
    '网格平仓估算待补盘口或账务',
    '行情数值不能为布尔值', '行情数值缺失或格式错误', '行情数值无效',
    '行情时间超前，等待同步报价',
    '接口返回错误格式或状态', 'Lighter order_books 缺失',
    'Lighter order_book_details 缺失',
])

# These are deterministic, non-sensitive local validation failures.  Keep
# them visible in the UI so an account precheck is not confused with a quote
# failure; raw SDK/browser exception text remains masked below.
SAFE_EXECUTION_REASONS = frozenset([
    '持仓平台组合缺失，必须先核对原仓位',
    '持仓平台组合未匹配，必须先核对原仓位',
    '同品种持仓平台组合不一致，必须先核对原仓位',
    '网格本轮平台组合不一致，禁止加仓',
    '历史中心样本不足30个，等待；不会自动改用零目标',
    '历史中心正在后台计算，等待有效样本',
    '历史中心有效样本不足30个或覆盖不足14.5分钟',
    '历史中心最近有效样本已过期，等待更新',
    '历史中心有效样本待更新',
    '网格本轮回归目标或方向不一致，禁止加仓',
    '已冻结回归目标与本次方向不一致',
    '回归目标不可用', '回归目标超出有效价差范围',
    '整轮资金费预算未知', '整轮资金费预算无效',
    '历史中心未知', '历史中心无效',
    '已冻结回归目标未知', '已冻结回归目标无效',
    '买平台费用未知', '买平台费用无效', '卖平台费用未知', '卖平台费用无效',
    '补齐回归保护参数无效', '目标回归保护价无效',
    '最低平仓净收益未知', '最低平仓净收益无效',
    '预计净回归空间超过策略上限，等待下一轮报价',
    '没有符合预计净回归空间与金额上限的深度',
    '本次数量的预计整轮净收益未达入场门槛',
    '重新报价后预计净回归空间不满足条件',
    '盘口冲击加实际执行窗口价格预算超过每腿滑点上限，首腿未提交',
    '当前路线缩量上限已变化，首腿未提交',
    '回补后缩量低于数量精度，首腿未提交',
    '账户核验期间第二腿报价恶化超过价格变动预算，首腿未提交',
    '首腿未提交：第二腿预期对冲余量不足',
    '首腿未提交：第二腿预检报价或费用无效',
    '首腿未提交：第二腿预检报价无效',
    'Arcus 价格档位规则无效',
    'Arcus 订单价格不符合当前价格档位步长',
    'Arcus 价格档位内没有符合预算的可成交限价',
    'Arcus 保护限价无效',
    'Arcus 交易价格边界无效，等待更新',
    'Arcus 可成交报价触及盘外交易边界，禁止提交',
    '报价时效、数量、合约或执行资格不匹配',
    '补齐对冲提交窗口超时，禁止发送订单',
    '账户身份未确认',
    '当前市场有活动订单，需先核对',
    '已有账户敞口，禁止叠加',
    '只减仓数量超过真实持仓',
    '平仓方向与持仓不符',
    '可用保证金不足以覆盖保守的一倍名义金额',
    'Lighter 平仓后账户快照缺失或过期',
    'Lighter 当前账户未确认空仓且无挂单',
    'Arcus 当前市场有活动订单，需先核对',
    'Arcus 已有账户敞口，禁止叠加',
    'Arcus 账户可用保证金字段缺失，未发送订单',
    'Arcus 可用保证金不足以覆盖保守的一倍名义金额',
    'Arcus 只减仓数量或方向与真实持仓不符',
    '未满足 Arcus 最小下单数量或金额',
    '上一笔平仓终态未确认，禁止重发',
    '平仓重试次数已达上限，需核对账户',
    '缺少账户持仓核验，禁止平仓重试',
    '账户持仓数量无效',
    '本地剩余持仓数量无效，需核对账户',
    '账户持仓核验已过期',
    '同市场持仓方向不一致',
    '账户持仓与剩余敞口不符，需核对手动成交',
    '重复平仓回执，禁止重复扣减持仓',
])

def latest_reference_failure(events):
    """Newest unresolved reference read, from a newest-first event window.

    Parallel reads have independent outcomes. Another venue completing its
    read cannot clear a failed stage; a newer success for that same symbol
    and stage does clear it.
    """
    seen = set()
    for event in events:
        detail = event.get('detail') or {}
        name = detail.get('stage', '')
        if event.get('event') not in ('stage_completed', 'stage_failed') or not name.endswith(' 参考价格'):
            continue
        key = (detail.get('symbol'), name)
        if key in seen:
            continue
        seen.add(key)
        if event['event'] == 'stage_failed':
            return event
    return None

def safe_reason(exc):
    from src.common.rate_limit import RateLimitError, DiscoveryThrottleError
    from src.common.discovery import DiscoveryCooldownError
    if isinstance(exc, (RateLimitError, DiscoveryThrottleError, DiscoveryCooldownError)):
        return str(exc)
    message=str(exc)
    tick_error = re.search(r"validation error on field 'price': ([0-9.eE+-]{1,40}) is not a multiple of tick size ([0-9.eE+-]{1,40}) for its price tier", message)
    if tick_error:
        return f'订单价格 {tick_error[1]} 不符合该价格档最小变动单位 {tick_error[2]}'
    prefix='点击前检查失败，未提交：'
    if message.startswith(prefix) and message[len(prefix):] in REASONS:
        return message
    submit_prefix='提交前账户检查失败，未发送订单：'
    if message.startswith(submit_prefix):
        detail = message[len(submit_prefix):]
        if detail in SAFE_EXECUTION_REASONS or re.fullmatch(
                r'Arcus 当前市场有活动订单：订单号 [A-Za-z0-9._:?\\-]{1,128}，方向 (?:buy|sell|\\?)，数量 [0-9.eE+\\-]{1,40}，只减仓 (?:是|否)',
                detail):
            return detail
    if re.fullmatch(r'行情请求失败: (HTTP [0-9]{3}|ReadTimeout|ConnectTimeout|ProxyError|ConnectionError|Timeout|SSLError)',message):
        return message
    if message in SAFE_MARKET_REASONS:
        return message
    if message in SAFE_EXECUTION_REASONS:
        return message
    if re.fullmatch(r'未到通道[上下]边界: -?[0-9]{1,9}\.[0-9]{2} / -?[0-9]{1,9}\.[0-9]{2} bps',message) or re.fullmatch(
            r'通道有效样本不足或覆盖不足14\.5分钟: [0-9]{1,8}/[0-9]{1,8}',message) or re.fullmatch(
            r'通道宽度 [0-9]{1,9}\.[0-9]{2} bps 超过允许上限，暂不入场',message):
        return message
    if re.fullmatch(r'首腿未提交：近期平仓盘口恶化风险 [0-9]{1,9}\.[0-9]{2} bps，'
                    r'扣除后预计对冲净空间 -?[0-9]{1,9}\.[0-9]{2} bps，未达收益门槛', message):
        return message
    if message in ('Lighter 市场保证金详情缺失或合约不匹配', 'Lighter 初始保证金设置无效',
                   'Lighter 账户市场设置重复，需核对'):
        return message
    if re.fullmatch(r'Lighter [A-Z0-9._-]{1,40} (?:未返回账户杠杆或初始保证金设置|账户已有市场记录但保证金设置缺失)', message):
        return message
    status = getattr(getattr(exc, 'response', None), 'status_code', None)
    if status is None and isinstance(getattr(exc, 'status', None), int):
        status = exc.status
    if isinstance(status, int) and 100 <= status <= 599:
        # Keep only the HTTP status; never expose response bodies, headers or
        # signed request details in the durable execution history.
        return f'接口请求失败: HTTP {status}'
    if re.fullmatch(r'(?:V|L|Lighter|Lighter RH|Arcus) 最小下单条件不满足: 数量 [0-9.eE+-]{1,40} < [0-9.eE+-]{1,40} 或名义金额 [0-9.eE+-]{1,40} < [0-9.eE+-]{1,40}', message):
        return message
    # Leverage failures include only the adapter name and a sanitized SDK
    # response code/message selected by execution.py; retain that context.
    if re.fullmatch(r'(?:Lighter|Lighter RH) 杠杆设置失败: [^\n]{1,160}', message):
        return message
    if message in ('Lighter 杠杆设置后未确认', 'Lighter RH 杠杆设置后未确认'):
        return message
    if re.fullmatch(r'Lighter 未上线永续市场 [A-Z0-9._-]+', message):
        return message
    if re.fullmatch(r'Arcus (?:未找到可交易市场|市场当前不可交易)：[A-Z0-9._-]{1,64}', message):
        return message
    return message if message in REASONS else type(exc).__name__+'：检查未通过，详见阶段和代码位置'

@contextmanager
def stage(history, name, symbol):
    start=time.monotonic()
    try:
        yield
    except Exception as exc:
        frame=traceback.extract_tb(exc.__traceback__)[-1]
        history.append('stage_failed',symbol=symbol,stage=name,reason=safe_reason(exc),
                       elapsed_ms=round((time.monotonic()-start)*1000),location=Path(frame.filename).name+':'+str(frame.lineno))
        raise
    else:
        history.append('stage_completed',symbol=symbol,stage=name,elapsed_ms=round((time.monotonic()-start)*1000))
