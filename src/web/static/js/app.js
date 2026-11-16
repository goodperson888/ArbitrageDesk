'use strict';
const $ = id => document.getElementById(id);
const money = x => Number.isFinite(x) ? (x<0?'-$':'$') + Math.abs(x).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: Math.abs(x) < 1 ? 6 : 2}) : '--';
const qty = x => Number.isFinite(x) ? x.toLocaleString('en-US', {maximumFractionDigits: 8}) : '--';
// APIs/history retain basis points; selected display controls use percent.
const toPercent = x => Number.isFinite(x) ? x / 100 : null;
const toBasisPoints = x => Number((x * 100).toFixed(10));
const percent = x => Number.isFinite(x) ? (x > 0 ? '+' : '') + toPercent(x).toFixed(4) : '--';
const basisPoints = x => Number.isFinite(x) ? (x > 0 ? '+' : '') + x.toFixed(2) + ' bps' : '--';
const durationText = x => Number.isFinite(x) ? Math.max(0, Math.round(x)) + ' ms' : '--';
const spreadText = x => Number.isFinite(x) ? basisPoints(x) + ' (' + (x / 100).toFixed(4) + '%)' : '--';
const percentFields = new Set(['slippage', 'grid-step']);
const timeText = x => x ? new Date(x).toLocaleTimeString('zh-CN', {hour12: false}) : '--';
const ageText = age => Number.isFinite(age) ? (age < 60 ? `${Math.max(0, age).toFixed(1)} 秒前` : `${(age/60).toFixed(1)} 分钟前`) : '时间未知';
let chart, events, latest = null, rows = [], lastEvent = 0, connectionGeneration = 0, historyGeneration = 0;
let executionHistoryView = 'all';
let executionHistoryPage = 1;
let executionHistoryPair = '';
let executionHistoryOrders = [];
let executionHistoryLoading = false, executionHistoryReload = false;
const executionHistoryPageSize = 20;
let platformHistory = null;
let retainedLossLimits = {};
let currentSymbol = '', lastHistorySample = 0, disconnected = false, activeHistoryPair = '', historyVenues = [], historyVenuesReady = false;
const venueNames = {variational:'Variational', lighter:'Lighter', lighter_rh:'Lighter RH', standx:'StandX', binance:'Binance Futures', gate:'Gate Futures', okx:'OKX Swap', arcus:'Arcus'};
const venueName = value => venueNames[value] || value || '未知平台';
const routeName = (buy, sell) => `${venueName(buy)} → ${venueName(sell)}`;
const timestampMs = value => {
    if (typeof value === 'number') return value < 1e12 ? value * 1000 : value;
    const parsed = Date.parse(value || '');
    return Number.isFinite(parsed) ? parsed : 0;
};

window.setHistoryPair = (buy, sell, symbol) => {
    activeHistoryPair = buy && sell ? `${buy}->${sell}` : '';
    if (symbol && symbol !== currentSymbol) {
        currentSymbol = symbol.replace(/-PERP$/, '');
        rows = [];
    }
    const select = $('history-pair');
    if (select) select.value = activeHistoryPair;
    loadHistory();
};
window.setHistoryVenues = venues => {
    historyVenuesReady = true;
    historyVenues = Array.isArray(venues) ? venues.slice() : [];
    if (activeHistoryPair) {
        const [buy, sell] = activeHistoryPair.split('->');
        if (!historyVenues.includes(buy) || !historyVenues.includes(sell)) {
            activeHistoryPair = '';
        }
    }
    renderChart();
};

async function api(path, payload) {
    const response = await fetch(path, payload === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
}
function pill(id, text, kind = 'neutral') { $(id).textContent = text; $(id).className = 'pill ' + kind; }
function formStatus(id, text, ok = false) {
    const node = $(id); if (!node) return;
    node.textContent = text; node.className = 'platform-form-status ' + (ok ? 'ok' : 'warn');
}
function edge(id, value) {
    const root = $(id); root.replaceChildren(document.createTextNode(percent(value)));
    const unit = document.createElement('small'); unit.textContent = '%'; root.appendChild(unit);
    root.className = 'edge ' + (value > 0 ? 'positive' : value < 0 ? 'negative' : '');
}
function clearLive(message) {
    latest = null;
    for (const name of ['variational','lighter']) {
        $(name+'-ask').textContent = '--'; $(name+'-bid').textContent = '--';
        $(name+'-mark').textContent = '--'; $(name+'-quote-age').textContent = message;
        pill(name+'-source', '无有效数据', 'warn');
    }
    edge('edge-a', null); edge('edge-b', null);
    $('depth-a').textContent = $('depth-b').textContent = '--';
    $('slippage-a').textContent = $('slippage-b').textContent = '--';
    $('slippage-source-a').textContent = $('slippage-source-b').textContent = '滑点观测';
    $('base-quantity').textContent = '--'; $('quantity-source').textContent = '';
    renderCost(null); renderAllocation(null); renderHedgeCost(null);
    $('quality-title').textContent = '数据连接中断';
    $('quality-note').textContent = message; $('market-notice').textContent = message;
}
function connect() {
    // The modern multi-venue dashboard has its own stream. Hidden legacy
    // controls must not keep a second pair of market feeds alive.
    if (document.querySelector?.('.legacy-controls[hidden]')) return;
    if (events) events.close();
    const generation = ++connectionGeneration;
    const symbol = $('market-select').value, notional = Number($('notional').value);
    if (!Number.isFinite(notional) || notional < 1 || notional > 1000000) {
        lastEvent = 0; clearLive('测算金额须在 1 至 1,000,000 USDC 之间');
        pill('stream-status', '请修正测算金额', 'warn'); return;
    }
    const changed = currentSymbol !== symbol;
    currentSymbol = symbol; disconnected = false; lastEvent = Date.now();
    clearLive('正在连接 '+symbol+' 实时行情…'); pill('stream-status','连接中','warn');
    if (changed) { rows = []; latest=null;lastHistorySample=0; renderChart(); loadHistory(); }
    events = new EventSource('/api/stream?symbol='+encodeURIComponent(symbol)+'&notional='+notional);
    events.addEventListener('quote', event => {
        if (generation !== connectionGeneration) return;
        try {
            const row = JSON.parse(event.data);
            if (row.symbol !== currentSymbol) return;
            lastEvent = Date.now(); disconnected = false; latest = row;
            pill('stream-status', '长连接已接通'); render(row); sample(row);
        } catch { pill('stream-status', '数据格式异常', 'error'); }
    });
    events.onerror = () => { if (generation === connectionGeneration) pill('stream-status', '断线 · 自动重连', 'warn'); };
}
function render(row) {
    const c = row.comparison, v = row.details?.variational || {}, l = row.details?.lighter || {};
    $('variational-instrument').textContent = (v.exchange_symbol || currentSymbol)+' / '+(v.instrument_type === 'swap' ? 'SWAP' : 'PERPETUAL');
    $('lighter-instrument').textContent = currentSymbol+' / PERPETUAL';
    for (const venue of ['variational','lighter']) {
        $(venue+'-ask').textContent = money(c[venue+'_ask']); $(venue+'-bid').textContent = money(c[venue+'_bid']);
        $(venue+'-mark').textContent = money(row[venue]);
    }
    const bridged = c.quote_source === 'variational_browser_quote';
    pill('variational-source', bridged ? '官网数量报价' : '缓存 · 非实时', bridged && c.fresh ? '' : 'warn');
    pill('lighter-source', row.book?.source === 'lighter_websocket' ? 'WebSocket 盘口' : row.book ? 'REST 盘口 · 降级' : '盘口不可用', row.book?.source === 'lighter_websocket' ? '' : 'warn');
    $('variational-quote-age').textContent = (bridged && v.quote_timestamp_kind === 'browser_observed' ? '网页观察 ' : '报价更新 ') + ageText(c.variational_quote_age);
    $('lighter-quote-age').textContent = '盘口接收 '+ageText(c.lighter_quote_age);
    $('variational-size').textContent = bridged ? '数量 '+qty(v.quote_size)+' '+currentSymbol : '参考档位 '+money(v.quote_notional_usdc);
    $('lighter-size').textContent = row.book ? '已获取 '+row.book.bids.length+' / '+row.book.asks.length+' 档' : '--';
    $('variational-mark-time').textContent = v.mark_source === 'variational_websocket' ? 'WS '+timeText(v.mark_updated_at) : 'REST 获取 '+timeText(v.fetched_at);
    $('lighter-mark-time').textContent = 'REST 获取 '+timeText(l.fetched_at);
    const vh = row.feeds.variational, lh = row.feeds.lighter;
    $('variational-feed').textContent = currentSymbol === 'US100' ? '此 Swap 使用 REST 参考价，未接入价格流' :
        v.mark_source === 'variational_websocket' ? '标记价 WebSocket 已连接 · 收到 '+vh.messages+' 条消息' :
        vh.error || '当前标记价使用 REST，等待新鲜 WebSocket 数据';
    $('lighter-feed').textContent = lh.connected ? '盘口 WebSocket 已连接 · 收到 '+lh.messages+' 条消息' : lh.error || '盘口流连接中';
    edge('edge-a', c.buy_variational_sell_lighter_bps); edge('edge-b', c.buy_lighter_sell_variational_bps);
    $('depth-a').textContent = percent(c.depth_buy_variational_sell_lighter_bps)+'%';
    $('depth-b').textContent = percent(c.depth_buy_lighter_sell_variational_bps)+'%';
    $('base-quantity').textContent = qty(c.base_quantity)+' '+currentSymbol;
    $('quantity-source').textContent = bridged ? '按网页报价数量比较；网页金额约 '+money(v.quote_notional_usdc)+'，以网页输入为准。' : c.quantity_source;
    $('quality-title').textContent = c.fresh ? (bridged ? '网页报价已收到 · 仍需成交核对' : '参考报价较新 · 非执行报价') : '仅作参考 · 报价不够新鲜';
    $('quality-note').textContent = c.reasons.join('；') || c.note;
    const selectedVenues = globalThis.getSelectedVenues?.() || [];
    const usesVariational = selectedVenues.includes('variational');
    $('market-notice').className = 'notice' + (usesVariational && (row.instrument_mismatch || !bridged) ? ' warn' : '');
    $('market-notice').textContent = usesVariational
        ? (row.instrument_mismatch ? '合约类型不一致 · 当前仅展示参考价' :
            (bridged ? 'Variational 数量报价已接收 · 提交前仍会重新核价' : (row.feeds.bridge.symbols.length ? '已收到其他市场报价 · 当前品种仍需在官网填入数量' : 'Variational 尚未收到当前品种 RFQ · 请保持官网市场页和桥接连接')))
        : selectedVenues.length === 2
            ? '已选两个平台行情已接收 · 实盘验证入口在下方“连接与实盘检查”'
        : selectedVenues.length > 2
            ? '已选多个平台行情已接收 · 实盘执行需在下方明确选择恰好两个平台'
            : '选择至少两个平台后开始比较行情';
    renderConnections(row); renderCost(row); renderAllocation(row.allocation); renderHedgeCost(row);
    renderObservedSlippage(row);
    const bridge = row.feeds.bridge;
    pill('bridge-state', bridge.symbols.length ? '已收到 '+bridge.symbols.join(' / ') : '等待网页报价', bridge.symbols.length ? '' : 'neutral');
    const rejection = bridge.last_rejection;
    $('bridge-note').textContent = rejection
        ? `最近拒绝：${rejection.reason}。这通常表示页面刚收到的是平台缓存报价。`
        : bridge.symbols.length ? '已接收 '+bridge.symbols.join(' / ')+'；最近报价 '+timeText(bridge.last_received_at)+'。官网需持续产生报价，面板才会更新。' : '未收到新报价：检查配对、官网市场与输入数量；页面休眠或断网也会中断报价。';
    $('last-observed').textContent = '面板接收 '+timeText(row.timestamp);
}
function renderHedgeCost(row){
    for(const name of ['a','b']){
        const d=row?.hedge_cost?.directions?.[name];
        const hc=row?.hedge_cost;
        $('fee-source-'+name).textContent='开仓手续费 · Variational '+(hc?.variational_fee_source==='website_quote_estimate'?'官网预计':'手动预测')+' / Lighter 核验';
        const funding=row?.details?.variational?.funding;
        const left=funding&&Number.isFinite(funding.next_in_s)?Math.max(0,funding.next_in_s-(Date.now()-Date.parse(funding.observed_at))/1000):null;
        $('funding-source-'+name).textContent='Variational 资金费 · '+(hc?.funding_source==='website_display'?'官网预计 '+percent(hc.funding_rate_bps)+'%':Number.isFinite(hc?.funding_rate_bps)?'手动预测':'未知')+(left===null?'':' · 距结算约 '+Math.ceil(left/60)+'分钟');
        $('direction-'+name+'-prices').textContent=d?.available?
            qty(d.quantity)+' '+row.symbol+' · 买 '+money(d.buy_price)+' / 卖 '+money(d.sell_price):'深度或报价不足';
        $('gap-'+name).textContent=d?.available?money(d.price_gap_usdc_per_unit):'--';
        $('notional-'+name).textContent=d?.available?money(d.entry_notional_usdc):'--';
        $('gross-'+name).textContent=d?.available?
            percent(d.gross_bps)+'% · '+money(d.gross_convergence_usdc ?? d.gross_usdc):'--';
        $('fees-'+name).textContent=Number.isFinite(d?.fees_usdc)?money(d.fees_usdc):Number.isFinite(row?.hedge_cost?.lighter_fee_bps)?'Variational 手续费预测未设置':'Lighter 账户费率待核验';
        $('funding-'+name).textContent=Number.isFinite(d?.funding_estimate_usdc)?(d.funding_estimate_usdc<0?'预计收取 ':d.funding_estimate_usdc>0?'预计支付 ':'预计 ')+money(Math.abs(d.funding_estimate_usdc))+' / '+((hc?.funding_interval_s||28800)/3600)+'小时':'未知';
        $('net-'+name).textContent=Number.isFinite(d?.after_known_entry_fees_usdc ?? d?.net_entry_usdc)?
            percent(d.net_entry_bps)+'%（'+basisPoints(d.net_entry_bps)+'） · '+money(d.after_known_entry_fees_usdc ?? d.net_entry_usdc):'未知 · 不按零费用估算';
        const netNote=$('net-note-'+name);
        if(netNote) netNote.textContent = Number.isFinite(d?.after_known_entry_fees_usdc ?? d?.net_entry_usdc)
            ? '已扣可核验开仓手续费；未扣滑点与未来资金费'
            : (hc?.unknown_reasons||[]).join('；') || '等待两平台费用证据';
    }
}
function renderCost(row) {
    const cost = row?.entry_cost;
    for (const side of ['buy','sell']) {
        const value = cost?.available ? cost[side] : null;
        const current = value?.quoted_cost_usdc;
        $('cost-'+side).textContent = Number.isFinite(current) ? (current>0?'+':'')+money(current)+' USDC' : '--';
        $('cost-'+side+'-parts').textContent = value ?
            '报价滑点 '+percent(value.quoted_slippage_bps)+'%' : '缺少双边报价或数量';
    }
    $('cost-spread').textContent = cost?.available ? percent(cost.full_spread_bps)+'%' : '--';
    $('cost-basis').textContent = cost?.available ? '往返点差约 '+money(cost.unchanged_roundtrip_spread_usdc) : '';
    pill('cost-status', !cost?.available ? '无法估算' : cost.aligned ? '当前报价估算' : '异步报价 · 仅参考', 'warn');
    $('cost-note').textContent = cost?.available ?
        '报价 '+ageText(cost.quote_age_seconds)+'；标记价 '+ageText(cost.mark_age_seconds)+'。'+
        (cost.aligned?'报价时间接近，仍不保证成交。':'两者时间未对齐，不代表官网当前显示滑点。')+
        (row.comparison.quote_source==='variational_browser_quote'?'使用网页实际报价数量。':'使用公开缓存档位，任意数量只作线性估算。') : '等待报价；不会把未知滑点填成零。';
    $('cost-decomposition').textContent = cost?.available ? '本地计算 · 数量 '+qty(cost.quantity)+' '+row.symbol+'：半点差成本 '+money(cost.buy.spread_cost_usdc)+'；买入中间价偏离 '+money(cost.buy.midpoint_deviation_usdc)+'；卖出中间价偏离 '+money(cost.sell.midpoint_deviation_usdc)+'。' : '暂无有效报价可拆分。';
    const page=row?.details?.variational?.page_slippage;
    if(page)$('cost-note').textContent+=' 官网当前'+(page.side==='buy'?'买入':'卖出')+'显示：预计 '+percent(page.estimated_bps)+'% / 上限 '+percent(page.max_bps)+'%（只读同步）。';
}
function renderObservedSlippage(row){
    const directions=row?.comparison?.slippage?.directions||{};
    for(const name of ['a','b']){
        const item=directions[name]||{};
        const parts=[];
        if(Number.isFinite(item.variational_bps))parts.push('Variational '+percent(item.variational_bps)+'%');
        if(Number.isFinite(item.lighter_bps))parts.push('Lighter '+percent(item.lighter_bps)+'%');
        $('slippage-'+name).textContent=parts.length?parts.join(' / ')+' · 合计 '+(Number.isFinite(item.observed_bps)?percent(item.observed_bps)+'%':'待补齐'):'暂无同数量观测';
        $('slippage-source-'+name).textContent=item.known_legs===2?'实时观测滑点（非成交回执）':'滑点观测（部分或缺失）';
    }
}
function renderAllocation(plan){
    $('allocation-cap').textContent=plan?'全局硬上限 '+money(plan.cap_usdc)+' USDC':'等待配置';
    $('allocation-status').textContent=plan?.status||'等待新鲜行情';
    const root=$('allocation-plans');root.replaceChildren();
    for(const item of plan?.plans||[]){
        const line=document.createElement('p');line.className='chart-note';
        const label=item.direction==='buy_variational'?'买入 Variational / 卖出 Lighter':'买入 Lighter / 卖出 Variational';
        const slippage = item.available ? (item.slippage_breakdown+'：Variational '+percent(item.variational_slippage_bps)+'% / Lighter '+percent(item.lighter_slippage_bps)+'% · 双腿合计 '+percent(item.slippage_budget_bps)+'%') : '';
        const mode = item.slippage_mode || '使用配置滑点预算';
        line.textContent=label+'：'+(item.available?qty(item.quantity)+' '+currentSymbol+' · Variational '+money(item.variational_notional)+' / Lighter '+money(item.lighter_notional)+' · 净价差（扣手续费及滑点预算） '+percent(item.net_edge_bps)+'%（'+slippage+'；'+mode+'；未扣 Lighter 账户费用）':item.reason);
        root.appendChild(line);
    }
}
// Navigate explicitly to every platform's open orders using the existing history loader.
globalThis.showOpenExecutionHistory = () => {
    executionHistoryView = 'open';
    executionHistoryPair = '';
    executionHistoryPage = 1;
    const panel = $('execution-history-panel');
    if (panel) {
        panel.open = true;
        panel.scrollIntoView({block:'start'});
        panel.querySelector('summary')?.focus({preventScroll:true});
    }
    return loadExecutionHistory();
};

async function loadExecutionHistory(){
    if(executionHistoryLoading){executionHistoryReload=true;return;}
    executionHistoryLoading=true;
    const selection=()=>[executionHistoryPair,executionHistoryView,executionHistoryPage].join('|');
    const requestedSelection=selection();
    try{
        const eventQuery=executionHistoryView==='events'?('?event_page='+executionHistoryPage+'&event_page_size='+executionHistoryPageSize):'?event_page_size=200';const data=await api('/api/execution-history'+eventQuery+(executionHistoryPair?'&venues='+encodeURIComponent(executionHistoryPair):''));if(requestedSelection!==selection()){executionHistoryReload=true;return;}const root=$('execution-history');
        // Capture after the request: the user may keep scrolling while it is in flight.
        const historyScroll={left:root.scrollLeft,top:root.scrollTop};
        const scrollPositions=new Map(Array.from(root.querySelectorAll('[data-history-scroll-key]'),wrap=>[wrap.dataset.historyScrollKey,{left:wrap.scrollLeft,top:wrap.scrollTop}]));
        root.replaceChildren();
        const summary=data.summary||{};
        const usdc=value=>Number.isFinite(value)?(value>0?'+':'')+value.toFixed(4)+' USDC':'待补';
        const text=value=>value==null||value===''?'--':String(value);
        const section=(title,subtitle)=>{const wrap=document.createElement('section');wrap.className='execution-history-section';const head=document.createElement('div');head.className='execution-history-section-head';const strong=document.createElement('strong');strong.textContent=title;const small=document.createElement('small');small.textContent=subtitle;head.append(strong,small);wrap.appendChild(head);return wrap;};
        const summaryBox=document.createElement('div');summaryBox.className='execution-history-summary';
        [['未平仓',summary.open_count||0],['已平仓',summary.closed_count||0],['双腿当日已实现净收益',usdc(summary.realized_pnl_usdc)],['未平仓估算',usdc(summary.estimated_unrealized_pnl_usdc)],['当日总计估算',usdc(summary.estimated_total_pnl_usdc)]].forEach(([label,value])=>{const item=document.createElement('div');const small=document.createElement('small');small.textContent=label;const b=document.createElement('b');b.textContent=value;item.append(small,b);summaryBox.appendChild(item);});
        root.appendChild(summaryBox);
        const scopeNote=document.createElement('p');scopeNote.className='chart-note';
        scopeNote.textContent=(data.scope==='pair'?(data.venues||[]).map(venueName).join(' ↔ '):'全部平台组合')+' · 待核账 '+(summary.pending_accounting_count||0)+' 组';scopeNote.title='历史范围独立于运行组合；收益仅统计可核算记录，其他回执在事件中查看。';root.appendChild(scopeNote);
        if(data.history_sync){
            const sync=data.history_sync,note=document.createElement('p');note.className='chart-note';
            const errors=(sync.errors||[]).map(item=>venueName(item.venue)+(item.error==='RateLimitError'?' 限流':item.stage?' '+item.stage+' 暂未读全':' 历史暂未读全')).join('；');
            note.textContent=(sync.message||'平台历史自动补齐')+(sync.checked_at?' · 最近补查 '+new Date(sync.checked_at).toLocaleString('zh-CN',{hour12:false}):'')+(sync.retry_after_seconds?' · '+sync.retry_after_seconds+' 秒后重试':'')+(errors?' · '+errors:'');
            root.appendChild(note);
        }
        for(const cycle of (data.grid_cycles||[]).slice(0,10)){
            const note=document.createElement('p');note.className='chart-note';
            note.textContent=`网格 ${cycle.symbol} · ${(cycle.venues||[]).map(venueName).join(' ↔ ')} · 已用 ${cycle.used_layers} 层 / 持仓 ${cycle.active_layers} 层 · 已实现 ${usdc(cycle.realized_pnl_usdc)} · 剩余持仓估算 ${usdc(cycle.estimated_unrealized_pnl_usdc)} · 整组总计估算 ${usdc(cycle.estimated_total_pnl_usdc)}（持仓估算未扣未来平仓费，账务缺失保持未知）`;
            root.appendChild(note);
        }
        if(data.history_errors?.length){const warning=document.createElement('p');warning.textContent='部分历史读取失败：'+data.history_errors.map(item=>item.file).join('、')+'；当前列表可能不完整。';root.appendChild(warning);}


        const toolbar=document.createElement('div');toolbar.className='execution-history-toolbar';
        const pairSelect=document.createElement('select');pairSelect.setAttribute('aria-label','历史平台范围');
        const allOption=document.createElement('option');allOption.value='';allOption.textContent='全部平台历史';pairSelect.appendChild(allOption);
        for(const pair of (data.available_pairs||[])){const option=document.createElement('option');option.value=pair.join(',');option.textContent=pair.map(venueName).join(' ↔ ');pairSelect.appendChild(option);}
        pairSelect.value=executionHistoryPair;pairSelect.addEventListener('change',()=>{executionHistoryPair=pairSelect.value;executionHistoryPage=1;loadExecutionHistory();});toolbar.appendChild(pairSelect);

        [['all','订单汇总'],['open','未平仓'],['closed','已平仓'],['attention','待核对'],['events','执行事件']].forEach(([view,label])=>{const button=document.createElement('button');button.type='button';button.className='secondary'+(executionHistoryView===view?' is-active':'');button.textContent=label;button.setAttribute('aria-pressed',executionHistoryView===view?'true':'false');button.addEventListener('click',()=>{executionHistoryView=view;executionHistoryPage=1;loadExecutionHistory();});toolbar.appendChild(button);});
        const platformButton=document.createElement('button');platformButton.type='button';platformButton.className='secondary';platformButton.textContent='刷新平台成交历史';platformButton.title='按历史平台范围补查成交、手续费和资金费；后台自动补齐与手动刷新共用单次查询';platformButton.addEventListener('click',loadPlatformHistory);toolbar.appendChild(platformButton);
        root.appendChild(toolbar);

        const isAttention=p=>Boolean(p.accounting_pending||p.pending_funding_venues?.length||p.pending_fee_venues?.length||p.status==='reconcile_required');
        // Sort the entire collection before pagination. An older open or
        // partially closed hedge must stay above newer completed rollbacks;
        // closed rows awaiting fee accounting do not displace live exposure.
        const orders=[...(data.open_positions||[]).map(p=>({...p,row_state:isAttention(p)?'attention':'open'})),...(data.closed_positions||[]).map(p=>({...p,row_state:isAttention(p)?'attention':'closed'}))].sort((a,b)=>Number(b.status!=='closed')-Number(a.status!=='closed')||timestampMs(b.closed_time||b.open_time)-timestampMs(a.closed_time||a.open_time));
        executionHistoryOrders=orders;
        globalThis.updateOpportunityHistoryPositions?.(orders);
        globalThis.updateDeskPositions?.(data);
        const eventRows=[...(data.events||[])].sort((a,b)=>timestampMs(b.timestamp)-timestampMs(a.timestamp)||(Number(b.id||0)-Number(a.id||0)));
        const dateText=value=>value?new Date(value).toLocaleString('zh-CN',{hour12:false}):'--';
        const platformLabel=value=>venueName(value||'');
        const legLabel=(p,first)=>platformLabel(first?(p.variational_venue||'variational'):(p.lighter_venue||'lighter'));
        const routeText=p=>{
            const firstSide=['long','buy'].includes(p.variational_side)?'买入':'卖出';
            const secondSide=['long','buy'].includes(p.lighter_side)?'买入':'卖出';
            return firstSide+' '+legLabel(p,true)+' ↔ '+secondSide+' '+legLabel(p,false);
        };
        const timingByExecution=new Map();
        for(const row of [...eventRows].reverse()){
            const d=row.detail||{}, id=d.execution_id;
            if(!id) continue;
            const timing=timingByExecution.get(id)||{total:null,steps:[],firstReceiptAt:null,secondSendDelay:null,hedgeReady:false};
            if(Number.isFinite(d.execution_total_ms)) timing.total=Math.max(timing.total||0,d.execution_total_ms);
            const stamp=timestampMs(row.timestamp);
            if(row.event==='entry_receipt'&&Number(d.filled_size)>0&&timing.firstReceiptAt===null&&stamp>0) timing.firstReceiptAt=stamp;
            if(row.event==='hedge_decision') timing.hedgeReady=d.decision==='hedge';
            if(['hedge_leg_skipped','close_receipt','single_leg_recovered'].includes(row.event)) timing.hedgeReady=false;
            if(row.event==='stage_started'&&String(d.stage||'').endsWith('签名发送')&&timing.hedgeReady&&timing.firstReceiptAt!==null&&stamp>=timing.firstReceiptAt&&timing.secondSendDelay===null) timing.secondSendDelay=stamp-timing.firstReceiptAt;
            if(Number.isFinite(d.elapsed_ms)){
                timing.steps.push({started:stamp-d.elapsed_ms,label:(d.stage||row.event)+' '+durationText(d.elapsed_ms)});
            }
            timingByExecution.set(id,timing);
        }
        for(const timing of timingByExecution.values()) timing.steps.sort((a,b)=>a.started-b.started);
        const addCell=(tr,value,className='')=>{const td=document.createElement('td');td.textContent=text(value);if(className)td.className=className;tr.appendChild(td);return td;};
        const pageNav=(total,page,onPage)=>{if(total<=1)return null;const nav=document.createElement('div');nav.className='execution-history-pager';const prev=document.createElement('button');prev.type='button';prev.className='secondary';prev.textContent='上一页';prev.disabled=page<=1;prev.onclick=()=>onPage(page-1);const info=document.createElement('span');info.textContent='第 '+page+' / '+total+' 页';const next=document.createElement('button');next.type='button';next.className='secondary';next.textContent='下一页';next.disabled=page>=total;next.onclick=()=>onPage(page+1);nav.append(prev,info,next);return nav;};
        const orderTable=(list)=>{const wrap=document.createElement('div');const totalPages=Math.max(1,Math.ceil(list.length/executionHistoryPageSize));executionHistoryPage=Math.min(executionHistoryPage,totalPages);const pageList=list.slice((executionHistoryPage-1)*executionHistoryPageSize,executionHistoryPage*executionHistoryPageSize);wrap.className='execution-history-table-wrap';const table=document.createElement('table');table.className='execution-history-table desk-order-table';const head=document.createElement('thead');const headRow=document.createElement('tr');const first=pageList[0]||list[0]||{};const mixed=new Set(pageList.map(p=>legLabel(p,true)+' / '+legLabel(p,false))).size>1;const firstName=mixed?'第一腿':legLabel(first,true),secondName=mixed?'第二腿':legLabel(first,false);const priceHeader=executionHistoryView==='closed'?'平仓价（'+firstName+' / '+secondName+'）':executionHistoryView==='open'?'当前标记价（'+firstName+' / '+secondName+'）':'标记价 / 平仓价（按订单状态）';const spreadHeader=executionHistoryView==='closed'?'平仓价差':executionHistoryView==='open'?'当前价差':'当前 / 平仓价差';['状态','时间','品种','对冲方向','数量（'+firstName+' / '+secondName+'）','入场价（'+firstName+' / '+secondName+'）','实际入场价差',spreadHeader,'执行总耗时','平仓成交耗时',priceHeader,'净盈亏','操作'].forEach(label=>{const th=document.createElement('th');th.textContent=label;if(label==='操作')th.className='history-actions';if(label==='时间')th.title='主行显示开仓时间，下行显示实际最后平仓成交时间';if(label===spreadHeader)th.title='每行显示与订单状态对应的一个双腿价差：持仓显示当前价差，已平仓显示实际平仓成交价差。';headRow.appendChild(th);});head.appendChild(headRow);table.appendChild(head);const body=document.createElement('tbody');if(!list.length){const tr=document.createElement('tr');const td=document.createElement('td');td.colSpan=13;td.className='execution-history-empty';td.textContent='当前筛选没有订单汇总。';tr.appendChild(td);body.appendChild(tr);}for(const p of pageList){const tr=document.createElement('tr');tr.className='execution-history-row';tr.tabIndex=0;tr.setAttribute('role','button');tr.setAttribute('aria-label','打开 '+text(p.symbol)+' 执行详情');const closedLike=p.row_state==='closed'||(p.row_state==='attention'&&p.status==='closed');const singleLeg=p.hedge_status==='single_leg_recovered';const status=singleLeg&&closedLike?(isAttention(p)?'已回补·待核账':'已回补·未成对'):p.row_state==='attention'?(closedLike?'已平仓·待核账':p.partially_closed?'部分已平·待核对':'待核对'):p.row_state==='closed'?'已平仓':'未平仓';const statusCell=addCell(tr,status,'history-status '+(singleLeg?'attention':p.row_state));if(singleLeg){statusCell.title=p.rollback_explanation?.text||'旧记录缺少回补详情；点击查看执行记录。';statusCell.setAttribute('aria-label',status+'：'+statusCell.title);}if(closedLike&&p.close_diagnostics?.outcome?.summary){const note=document.createElement('small');note.className='history-accounting-note history-close-outcome';note.textContent=p.close_diagnostics.outcome.summary;note.title=p.close_diagnostics.outcome.text||'';statusCell.appendChild(note);statusCell.title=p.close_diagnostics.text||note.title;}if(p.history_recovered){const note=document.createElement('small');note.className='history-accounting-note';note.textContent='平台成交已补齐';statusCell.appendChild(note);}if(!closedLike){const note=document.createElement('small');note.className='history-accounting-note';note.textContent=globalThis.positionMonitorBrief?.(p.position_monitor)||'持仓监控心跳待接入';note.title=globalThis.positionMonitorRowText?.(p.position_monitor)||'';statusCell.appendChild(note);}const timeCell=addCell(tr,dateText(p.open_time));if(closedLike&&p.closed_time){const note=document.createElement('small');note.className='history-accounting-note';note.textContent='平仓 '+dateText(p.closed_time);timeCell.appendChild(note);}addCell(tr,p.symbol+(p.grid_layer?' · 网格第 '+p.grid_layer+' 层':''));addCell(tr,routeText(p));const sizeV=closedLike?p.entry_size_variational:p.variational_size;const sizeL=closedLike?p.entry_size_lighter:p.lighter_size;const quantityCell=addCell(tr,qty(sizeV)+' / '+qty(sizeL));quantityCell.title=closedLike?'本单原开仓数量；本单剩余数量为 0 / 0':'本单当前剩余数量';const entryPriceCell=addCell(tr,money(p.entry_price_variational)+' / '+money(p.entry_price_lighter));if(p.entry_accounting&&Object.keys(p.entry_accounting).length){entryPriceCell.title='已按平台同订单、同方向、完整成交数量核验入场成本。';if(p.entry_accounting_pending_reload){const note=document.createElement('small');note.className='history-accounting-note';note.textContent='成交成本已校正 · 监控待加载';note.title='页面已使用平台实际成交价；当前执行进程仍使用旧成本，下次正常重启实盘后接管并加载校正。';entryPriceCell.appendChild(note);}}const entrySpreadCell=addCell(tr,spreadText(p.actual_entry_spread_bps));entrySpreadCell.title='按双腿实际开仓成交均价计算；与扣除整轮成本的预计净回归空间口径不同。';if(p.entry_target){entrySpreadCell.title+='\n冻结回归目标：'+(p.entry_target.target_mode==='center'?'历史中心 ':'零目标 ')+spreadText(p.entry_target.target_bps)+'；目标冻结于 '+dateText(p.entry_target.frozen_at)+'。';if(Number.isFinite(p.target_entry_net_bps))entrySpreadCell.title+='\n按入场成交估算的净回归空间：'+spreadText(p.target_entry_net_bps)+'，包含预计平仓成本及资金费预算；不等于最终已实现盈亏。';}const markReason=p.mark_error||'行情待更新';const lastEstimate=p.last_estimate;const spreadValue=closedLike?(Number.isFinite(p.close_spread_bps)?spreadText(p.close_spread_bps):'待补成交'):(Number.isFinite(p.current_spread_bps)?spreadText(p.current_spread_bps):Number.isFinite(lastEstimate?.spread_bps)?'上次 '+spreadText(lastEstimate.spread_bps)+' · 已失效':markReason);const spreadCell=addCell(tr,singleLeg?'不适用':(closedLike?'平仓：':'当前：')+spreadValue);if(!closedLike)spreadCell.title=(p.mark_note||'按双腿盘口中间价估算')+(p.mark_error?' '+p.mark_error:'');if(closedLike)spreadCell.title=singleLeg?'只有一条腿成交并回补，没有双腿平仓价差':(p.recovered_close_fills?'按平台实际平仓成交均价计算，沿原开仓方向显示；两腿成交时间可能不同，费用和资金费另核账。'+(p.close_fill_times?' '+Object.entries(p.close_fill_times).map(([leg,stamp])=>legLabel(p,leg==='variational')+' '+dateText(stamp)+(p.close_fill_time_sources?.[leg]==='receipt_received'?'（回执收到时间）':'')).join(' / '):''):'按两腿实际平仓成交均价计算；缺失成交证据时等待补查。');const timing=timingByExecution.get(p.execution_id);const elapsedCell=addCell(tr,durationText(p.execution_elapsed_ms??timing?.total));elapsedCell.title=(timing?.secondSendDelay!=null?'首腿确认→次腿发送 '+durationText(timing.secondSendDelay)+'；':'')+'从开始执行到双腿回执确认，含发送后等候；各阶段可能并行或嵌套，不可相加。';if(timing?.secondSendDelay!=null){const note=document.createElement('small');note.className='history-accounting-note';note.textContent='次腿发送 +'+durationText(timing.secondSendDelay);elapsedCell.appendChild(note);}const closeTimeCell=addCell(tr,closedLike?(p.history_recovered&&p.close_elapsed_ms>=60000?Math.floor(p.close_elapsed_ms/60000)+' 分 '+((p.close_elapsed_ms%60000)/1000).toFixed(1)+' 秒':durationText(p.close_elapsed_ms)):'--');if(Number.isFinite(p.close_accounting_ms)){closeTimeCell.title='双腿成交确认 '+durationText(p.close_execution_ms??p.close_elapsed_ms)+'；账务补齐总跨度 '+durationText(p.close_accounting_ms)+'，包含后台等待';}else if(p.status==='closed'&&(p.pending_funding_venues?.length||p.pending_fee_venues?.length)){closeTimeCell.title='仓位已按回执结束；费用或资金费正在后台核对，不包含在成交耗时中';}if(p.history_recovered&&Number.isFinite(p.close_elapsed_ms)){const note=document.createElement('small');note.className='history-accounting-note';note.textContent='含手动处理等待';closeTimeCell.appendChild(note);closeTimeCell.title='从程序开始平仓到最后一笔平台成交的总跨度，包含中途等待和手动平仓时间，不等于接口延迟';}const mark=closedLike?money(p.close_price_variational)+' / '+money(p.close_price_lighter):(p.mark_status==='live'?money(p.mark_price_variational)+' / '+money(p.mark_price_lighter):lastEstimate?.prices?'上次 '+lastEstimate.prices.map(money).join(' / ')+' · 已失效':markReason);const markCell=addCell(tr,mark);if(p.mark_updated_at)markCell.title='标记价更新于 '+dateText(p.mark_updated_at);const pnl=closedLike?p.realized_pnl_usdc:p.unrealized_pnl_usdc;const pnlCell=addCell(tr,Number.isFinite(pnl)?usdc(pnl):closedLike&&Number.isFinite(p.accounting_subtotal_usdc)?'暂计 '+usdc(p.accounting_subtotal_usdc):closedLike?'缺成交明细':Number.isFinite(lastEstimate?.unrealized_pnl_usdc)?'上次 '+usdc(lastEstimate.unrealized_pnl_usdc)+' · 已失效':(p.pnl_error||markReason),Number.isFinite(pnl)?(pnl>=0?'pnl-positive':'pnl-negative'):'');if(!closedLike){pnlCell.title=p.mark_note||'浮动盈亏估算：已扣已知入场手续费，未扣未来平仓手续费和待结算资金费。';const note=document.createElement('small');note.className='history-accounting-note';note.textContent=(p.partially_closed?'已实现＋剩余浮动估算':'中间价浮盈（非平仓收益）')+' · 未扣未来平仓费';pnlCell.appendChild(note);if(lastEstimate&&!Number.isFinite(pnl)){pnlCell.className='history-estimate-stale';pnlCell.title='最后有效估值 '+dateText(lastEstimate.updated_at)+'；当前行情失效，不能据此判断平仓条件。';}const exitNote=document.createElement('small');exitNote.className='history-accounting-note';const executableExit=globalThis.positionMonitorExitSummary?.(p.position_monitor);exitNote.textContent=executableExit?.text||(Number.isFinite(p.estimated_exit_pnl_usdc)?'盘口平仓估算 '+usdc(p.estimated_exit_pnl_usdc)+' · 尚未扣价格保护预算':'平仓估算待补报价或费率');exitNote.title=executableExit?.title||p.exit_estimate_note||'按平仓方向买卖价估算；最终需核验数量深度及价格保护。';pnlCell.appendChild(exitNote);}if(closedLike){pnlCell.title=(Number.isFinite(p.gross_price_pnl_usdc)?'价差盈亏 '+usdc(p.gross_price_pnl_usdc)+'；已核验手续费 '+usdc(p.known_fees_usdc)+'；已核验资金费 '+usdc(p.known_funding_usdc)+'；':'')+(p.accounting_note||'')+(p.close_diagnostics?.outcome?.text?'\n'+p.close_diagnostics.outcome.text:'');if(p.accounting_pending){const note=document.createElement('small');note.className='history-accounting-note';note.textContent=(p.accounting_missing||[]).map(item=>item.replace(/（订单 .*?）/g,'')).join('；')||p.accounting_note||'账单未完整';pnlCell.appendChild(note);}}const actionCell=addCell(tr,'—','history-actions');if(!closedLike){actionCell.textContent='';globalThis.appendManualCloseAction?.(p,actionCell);}const handoffText=timing?.secondSendDelay!=null?'首腿确认→次腿发送 '+durationText(timing.secondSendDelay)+'；':'';const stepText=timing?.steps?.length?'阶段按开始顺序（含并行或嵌套，不可相加）：'+timing.steps.map(step=>step.label).join('；'):'';const orderText=[...(p.entry_order_ids||[]).map(id=>'入场订单 '+id),...(p.close_order_ids||[]).map(id=>'平仓订单 '+id)];if(stepText||isAttention(p)||singleLeg||orderText.length||p.close_reason)tr.title=(timing?('执行总耗时 '+durationText(p.execution_elapsed_ms??timing.total)+'；'):'')+handoffText+stepText+(singleLeg?(closedLike?'；只有一条腿成交，另一条腿未成对，已回补':'；仅一条腿成交，剩余敞口待处理'):'')+(p.close_reason?'；平仓原因：'+p.close_reason:'')+(isAttention(p)?'；账务待核对：'+(p.accounting_note||'费用或资金费尚未完整'):'')+(orderText.length?'；'+orderText.join('；'):'');tr.addEventListener('click',()=>globalThis.openOpportunityHistoryDrawer?.(p));tr.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();globalThis.openOpportunityHistoryDrawer?.(p);}});body.appendChild(tr);}table.appendChild(body);wrap.appendChild(table);const pager=pageNav(totalPages,executionHistoryPage,page=>{executionHistoryPage=page;loadExecutionHistory();});if(pager)wrap.appendChild(pager);return wrap;};

        if(executionHistoryView!=='events'){
            const list=executionHistoryView==='all'?orders:orders.filter(p=>executionHistoryView==='attention'?isAttention(p):executionHistoryView==='closed'?p.status==='closed':p.status!=='closed');
            const title=executionHistoryView==='all'?'订单汇总':executionHistoryView==='open'?'未平仓订单':executionHistoryView==='closed'?'已平仓订单':'待核对订单';
            const splitHint=`完整双腿 ${summary.paired_closed_count??'--'} 组 · 单腿回补 ${summary.single_leg_recovered_count??'--'} 组`;
            const subtitle=splitHint;
            const orderSection=section(title,subtitle);const tableWrap=orderTable(list);tableWrap.dataset.historyScrollKey='orders';orderSection.appendChild(tableWrap);root.appendChild(orderSection);
        } else {
            const names={quote_evaluation:'同数量价差测算',stage_completed:'阶段完成',stage_failed:'阶段失败',plan_preview:'只读计划复测',planned:'发现机会',blocked:'跳过 / 阻断',hedge_leg_skipped:'第二腿保护拦截 · 进入回补处理',hedge_decision:'首腿成交后对冲评估',route_rollback_recorded:'已记录路线回补',route_rollback_paused:'连续回补 · 该路线已暂停',entry_receipt:'入场回执',platform_order_observed:'平台回执只读补查',close_receipt:'平仓回执',close_batch_started:'并发发送平仓',close_residual_retry:'已核对残余仓位 · 有限重试',close_retry_blocked:'平仓重试受阻 · 需核对',close_receipt_unknown:'平仓回执未知 · 需核对',hedged:'双腿配对完成',closed_pending_funding:'已平仓 · 资金费待补',single_attempt_started:'单次验证开始',single_attempt_skipped:'单次验证跳过',single_attempt_stopped:'单次验证停止',wait_once_started:'自动等待开始',wait_once_skipped:'等待中 · 本轮未达标',wait_once_stopped:'自动等待停止',position_reconcile_failed:'账户持仓复核失败 · 暂停平仓',position_resumed:'原仓位已接管 · 继续监控',external_close_detected:'检测到手动平仓 · 已暂停',external_position_mismatch:'检测到持仓不一致 · 已暂停'};
            const eventsSection=section('执行事件', '按时间倒序记录两腿回执、计划、跳过和异常；订单汇总请切回“订单汇总”');
            const list=document.createElement('div');list.className='execution-history-list';const totalPages=Math.max(1,Math.ceil(Number(data.events_total??eventRows.length)/executionHistoryPageSize));executionHistoryPage=Math.min(executionHistoryPage,totalPages);const pageRows=eventRows;
            for(const row of pageRows){
                const d=row.detail||{}, card=document.createElement('article');card.className='execution-history-card '+(['entry_receipt','hedged','close_receipt'].includes(row.event)?'success':(['blocked','close_retry_blocked','close_receipt_unknown','hedge_leg_skipped','stage_failed','single_attempt_skipped','wait_once_skipped','single_attempt_stopped','wait_once_stopped','position_reconcile_failed','external_close_detected','external_position_mismatch'].includes(row.event)?'warning':''));
                const head=document.createElement('div');head.className='execution-history-head';const title=document.createElement('strong');title.textContent=names[row.event]||row.event;head.appendChild(title);const time=document.createElement('time');time.dateTime=row.timestamp||'';time.textContent=dateText(row.timestamp);head.appendChild(time);card.appendChild(head);
                const tags=document.createElement('div');tags.className='execution-history-tags';[[d.symbol,'品种'],[d.venues?.join?.(' ↔ '),'平台'],[d.venue,'平台'],[d.direction,'方向'],[d.stage,'阶段'],[d.status,'状态']].forEach(([value,label])=>{if(value==null||value==='')return;const tag=document.createElement('span');tag.textContent=label+' '+value;tags.appendChild(tag);});if(tags.children.length)card.appendChild(tags);
                const timing=timingByExecution.get(d.execution_id);const metrics=document.createElement('div');metrics.className='execution-history-metrics';[[d.cap_usdc!=null?'每腿上限':'',d.cap_usdc!=null?money(d.cap_usdc):''],[d.quantity!=null?'数量':'',d.quantity!=null?qty(d.quantity):''],[d.filled_size!=null?'实际成交':'',d.filled_size!=null?qty(d.filled_size):''],[d.average_price!=null?'成交均价':'',d.average_price!=null?money(d.average_price):''],[d.fees_usdc!=null?'手续费':'',d.fees_usdc!=null?money(d.fees_usdc):''],[d.net_edge_bps!=null?'预计净价差':'',d.net_edge_bps!=null?percent(d.net_edge_bps)+'%':''],[d.actual_spread_bps!=null?'实际入场价差':'',d.actual_spread_bps!=null?spreadText(d.actual_spread_bps):''],[d.price_movement_bps!=null?'每腿变价预算':'',d.price_movement_bps!=null?basisPoints(d.price_movement_bps):''],[d.observed_price!=null?'第二腿新报价':'',d.observed_price!=null?money(d.observed_price):''],[d.protected_bound!=null?'对冲限价':'',d.protected_bound!=null?money(d.protected_bound):''],[d.hedge_edge_usdc!=null?'对冲价差净值（非结算）':'',d.hedge_edge_usdc!=null?usdc(d.hedge_edge_usdc):''],[d.unwind_pnl_estimate_usdc!=null?'预计立即回补损益':'',d.unwind_pnl_estimate_usdc!=null?usdc(d.unwind_pnl_estimate_usdc):''],[d.rollback_count!=null?'连续回补':'',d.rollback_count!=null?String(d.rollback_count):''],[d.paused_until?'暂停至':'',d.paused_until?dateText(d.paused_until*1000):''],[d.elapsed_ms!=null?'本步耗时':'',d.elapsed_ms!=null?durationText(d.elapsed_ms):''],[d.execution_total_ms!=null?'执行总耗时':'',d.execution_total_ms!=null?durationText(d.execution_total_ms):(timing?.total!=null?durationText(timing.total):'')],[d.first_submit_ms!=null?'首腿提交/回执':'',d.first_submit_ms!=null?durationText(d.first_submit_ms):''],[d.first_receipt_processing_ms!=null?'首腿回执处理':'',d.first_receipt_processing_ms!=null?durationText(d.first_receipt_processing_ms):''],[d.first_receipt_to_second_quote_ms!=null?'首腿确认→报价':'',d.first_receipt_to_second_quote_ms!=null?durationText(d.first_receipt_to_second_quote_ms):''],[d.second_quote_ms!=null?'第二腿报价':'',d.second_quote_ms!=null?durationText(d.second_quote_ms):''],[d.second_decision_ms!=null?'第二腿决策':'',d.second_decision_ms!=null?durationText(d.second_decision_ms):''],[d.first_receipt_to_second_send_ms!=null?'首腿确认→第二腿发送':'',d.first_receipt_to_second_send_ms!=null?durationText(d.first_receipt_to_second_send_ms):''],[d.second_submit_ms!=null?'第二腿提交/回执':'',d.second_submit_ms!=null?durationText(d.second_submit_ms):''],[d.order_id?'订单号':'',d.order_id||'']].forEach(([label,value])=>{if(!label)return;const item=document.createElement('span');const caption=document.createElement('small');caption.textContent=label;const valueNode=document.createElement('b');valueNode.textContent=value;item.append(caption,valueNode);metrics.appendChild(item);});if(metrics.children.length)card.appendChild(metrics);
                const note=d.reason||d.message||d.note||d.slippage_breakdown||'';if(note){const p=document.createElement('p');p.className='execution-history-note';p.textContent=note;card.appendChild(p);}list.appendChild(card);
            }
            if(!list.children.length){const empty=document.createElement('p');empty.className='execution-history-empty';empty.textContent='尚无执行事件。';list.appendChild(empty);}eventsSection.appendChild(list);const pager=pageNav(totalPages,executionHistoryPage,page=>{executionHistoryPage=page;loadExecutionHistory();});if(pager)eventsSection.appendChild(pager);root.appendChild(eventsSection);
        }
        if(data.other_fills?.length){
            const fillsSection=section('其他成交回执（含单平台测试）','以下是本程序保存的真实成交回执，未归入上述双腿订单；单平台测试不计入双腿净收益。资金费另需核账。');
            const wrap=document.createElement('div');wrap.className='execution-history-table-wrap';const table=document.createElement('table');table.className='execution-history-table';
            const head=document.createElement('tr');['时间','平台','品种','用途','方向','实际成交数量','成交均价','手续费','状态','订单号'].forEach(label=>{const th=document.createElement('th');th.textContent=label;head.appendChild(th);});table.appendChild(head);
            for(const row of data.other_fills){const tr=document.createElement('tr');[row.timestamp?dateText(row.timestamp):'时间未记录',venueName(row.venue),row.symbol,(row.source==='arcus_round_trip'?'单平台测试 · ':'其他成交 · ')+(row.reduce_only?'只减仓平仓':'开仓'),['buy','long'].includes(row.side)?'买入':'卖出',qty(row.filled_size),money(row.average_price),usdc(row.fees_usdc),row.status,row.order_id].forEach(value=>addCell(tr,value));table.appendChild(tr);}
            wrap.dataset.historyScrollKey='other-fills';wrap.appendChild(table);fillsSection.appendChild(wrap);const otherDisclosure=document.createElement('details');otherDisclosure.className='desk-disclosure';const otherSummary=document.createElement('summary');otherSummary.textContent='其他成交与单平台测试 · '+data.other_fills.length+' 条';otherDisclosure.append(otherSummary,fillsSection);root.appendChild(otherDisclosure);
        }
        if(platformHistory){
            const platformSection=section('平台成交历史（只读）','直接读取所选平台的实际成交历史；它与本地双腿汇总分开显示，避免把未成交或未匹配的单腿误算成一组套利订单。');
            const note=document.createElement('p');note.className='chart-note';note.textContent=platformHistory.message||((platformHistory.rows||[]).length?'查询完成；平台返回的每条订单保留原始状态。':'平台没有返回历史订单。');platformSection.appendChild(note);
            const tableWrap=document.createElement('div');tableWrap.className='execution-history-table-wrap';const table=document.createElement('table');table.className='execution-history-table';const head=document.createElement('tr');['平台','品种','状态','方向','初始数量','已成交数量','成交金额','订单号','时间'].forEach(label=>{const th=document.createElement('th');th.textContent=label;head.appendChild(th);});table.appendChild(head);
            for(const row of (platformHistory.rows||[])){const tr=document.createElement('tr');const values=[venueName(row.venue),row.symbol,row.status,row.side==='sell'||row.is_ask===true?'卖出':'买入',row.initial_base_amount??'--',row.filled_base_amount??row.filled_size??'--',row.filled_quote_amount??(row.average_price&&row.filled_size?money(Number(row.average_price)*Number(row.filled_size)):'--'),row.order_id||row.order_index||'--',row.timestamp||row.updated_at||row.created_at||'--'];for(const value of values){const td=document.createElement('td');td.textContent=value==null?'--':String(value);tr.appendChild(td);}table.appendChild(tr);}
            tableWrap.dataset.historyScrollKey='platform-history';tableWrap.appendChild(table);platformSection.appendChild(tableWrap);root.appendChild(platformSection);
        }
        root.querySelectorAll('[data-history-scroll-key]').forEach(wrap=>{const saved=scrollPositions.get(wrap.dataset.historyScrollKey);if(saved){wrap.scrollLeft=saved.left;wrap.scrollTop=saved.top;}});
        root.scrollLeft=historyScroll.left;root.scrollTop=historyScroll.top;
    }catch(e){if(requestedSelection===selection())$('execution-history').textContent='历史读取失败：'+e.message;}
    finally{executionHistoryLoading=false;if(executionHistoryReload){executionHistoryReload=false;loadExecutionHistory();}}
}
async function loadPlatformHistory(){
    const venues=executionHistoryPair?executionHistoryPair.split(','):(globalThis.getSelectedVenues?.()||[]);
    const selection=globalThis.getExecutionSelection?.()||{};
    const historical=executionHistoryOrders.filter(p=>venues.includes(p.variational_venue)&&venues.includes(p.lighter_venue)).map(p=>p.symbol);
    const symbols=[...new Set(historical.length?historical:(Array.isArray(selection.symbols)?selection.symbols:[]))];
    const token=document.querySelector('meta[name="local-token"]')?.content||'';
    if(venues.length!==2||symbols.length===0){platformHistory={success:false,message:'请先选择两个平台和至少一个品种。',rows:[]};loadExecutionHistory();return;}
    const button=[...document.querySelectorAll('#execution-history button')].find(item=>item.textContent==='刷新平台成交历史');if(button){button.disabled=true;button.textContent='读取平台历史中…';}
    try{const response=await fetch('/api/execution/platform-history',{method:'POST',headers:{'Content-Type':'application/json','X-Local-Token':token},body:JSON.stringify({venues,symbols,limit:100}),cache:'no-store'});const data=await response.json();if(!response.ok)throw new Error(data.message||data.error||('HTTP '+response.status));platformHistory=data;}catch(error){platformHistory={success:false,message:'平台历史读取失败：'+(error.message||error),rows:[]};}finally{loadExecutionHistory();}
}
function renderConnections(row) {
    const root = $('connection-list'); root.replaceChildren();
    const lines = [['Variational 价格流', '实时标记价 / 指数价', row.feeds.variational], ['Lighter 盘口流', '快照 + 增量 · 断线重新同步',row.feeds.lighter]];
    for (const [title, subtitle, h] of lines) {
        const node = document.createElement('div'); node.className = 'connection-row';
        const label = document.createElement('div'); label.textContent = title;
        const small = document.createElement('small'); small.textContent = subtitle; label.appendChild(small);
        const state = document.createElement('div'); state.className = 'connection-state '+(h.connected ? 'positive' : 'negative');
        state.textContent = h.connected ? '已连接 · '+h.messages+' 条 / 重连 '+h.reconnects+' 次'+(h.timeouts?' · 心跳 '+h.timeouts+' 次':'') : (h.error || '连接中')+(h.last_disconnect_reason?' · '+h.last_disconnect_reason:'');
        node.append(label,state); root.appendChild(node);
    }
}
function initChart() {
    if (!window.echarts) { $('spreadChart').textContent='本地图表资源缺失，请重新安装项目。';return; }
    chart=echarts.init($('spreadChart'),null,{renderer:'canvas'});
    chart.setOption({backgroundColor:'transparent',animation:false,
        color:['#5ce1c2','#8da9f2','#f9a69f','#e9bb67','#c6b5ff','#899bb8'],
        tooltip:{trigger:'axis',confine:true,backgroundColor:'#152338',borderColor:'#3b506b',textStyle:{color:'#ecf3ff'},
            valueFormatter:value=>Number.isFinite(value)?value.toFixed(4)+'%':'--'},
        legend:{top:2,left:0,textStyle:{color:'#aabbcf',fontSize:10},data:[]},
        grid:{left:64,right:15,top:42,bottom:48},
        xAxis:{type:'time',axisLine:{lineStyle:{color:'#344358'}},axisLabel:{color:'#8c9fb8',fontSize:10},splitLine:{show:false}},
        yAxis:{type:'value',scale:true,axisLabel:{color:'#8c9fb8',fontSize:10,formatter:'{value}%'},splitLine:{lineStyle:{color:'#243146'}}},
        dataZoom:[{type:'inside',filterMode:'none'},{type:'slider',height:16,bottom:4,borderColor:'#334258',backgroundColor:'#132033',fillerColor:'#5ce1c212',textStyle:{color:'#91a5bd'},handleStyle:{color:'#4a6b7f'}}],
        series:[]});
    new ResizeObserver(()=>chart.resize()).observe($('spreadChart'));
}
function normaliseHistoryRow(row) {
    if (row?.pair_key) {
        const legacyRatio = row.spread_basis === 'buy_ask';
        const value = row.gross_bps;
        return {...row, timestamp:row.timestamp, canonical_axis:legacyRatio,
            gross_bps:legacyRatio && Number.isFinite(value) && value>0 ? value/(1+value/10000) : value};
    }
    const timestamp = row?.timestamp;
    const legacy = [];
    if (Number.isFinite(row?.buy_variational_sell_lighter_bps)) legacy.push({
        timestamp, pair_key:'variational->lighter', buy_venue:'variational', sell_venue:'lighter',
        gross_bps:row.buy_variational_sell_lighter_bps,
        reference_bps:row.reference_buy_variational_sell_lighter_bps,
        buy_age_seconds:row.variational_quote_age, sell_age_seconds:row.lighter_quote_age,
        executable:row.quote_source === 'variational_browser_quote'
    });
    if (Number.isFinite(row?.buy_lighter_sell_variational_bps)) legacy.push({
        timestamp, pair_key:'lighter->variational', buy_venue:'lighter', sell_venue:'variational',
        gross_bps:row.buy_lighter_sell_variational_bps,
        reference_bps:row.reference_buy_lighter_sell_variational_bps,
        buy_age_seconds:row.lighter_quote_age, sell_age_seconds:row.variational_quote_age,
        executable:row.quote_source === 'variational_browser_quote'
    });
    return legacy;
}
function renderChart() {
    const title = $('chart-title');
    if (title) title.textContent = (activeHistoryPair ? routeName(...activeHistoryPair.split('->')) : '所选平台') + '价差趋势';
    if (!chart) return;
    const cutoff=Date.now()-Number($('history-range').value)*1000;
    const reference=$('chart-mode').value==='reference';
    const points = rows.flatMap(normaliseHistoryRow).filter(row => {
        const time = timestampMs(row.timestamp);
        if (!time || time < cutoff) return false;
        if (activeHistoryPair && row.pair_key !== activeHistoryPair) return false;
        if (!activeHistoryPair && row.canonical_axis && row.buy_venue > row.sell_venue) return false;
        if (historyVenuesReady && (!historyVenues.length || !historyVenues.includes(row.buy_venue) || !historyVenues.includes(row.sell_venue))) return false;
        if (!reference && (Number(row.buy_age_seconds) > 10 || Number(row.sell_age_seconds) > 10)) return false;
        return true;
    });
    const groups = new Map();
    points.forEach(row => { if (!groups.has(row.pair_key)) groups.set(row.pair_key, []); groups.get(row.pair_key).push(row); });
    const series = [...groups.entries()].map(([key, items], index) => ({
        name: routeName(items[0].buy_venue, items[0].sell_venue),
        type:'line', showSymbol:!reference, symbolSize:4, connectNulls:false, smooth:false,
        lineStyle:{width:1.8, type:reference?'dashed':'solid'},
        data:items.map(row => ({value:[timestampMs(row.timestamp), toPercent(reference && Number.isFinite(row.reference_bps) ? row.reference_bps : row.gross_bps)], quoteAge:Math.max(Number(row.buy_age_seconds)||0, Number(row.sell_age_seconds)||0)})),
        markLine:index===0?{silent:true,symbol:'none',label:{show:false},lineStyle:{color:'#596e89',type:'dotted'},data:[{yAxis:0}]}:undefined
    }));
    chart.setOption({legend:{data:series.map(item=>item.name)},series,
        tooltip:{formatter:params=>{
            const point=params[0];if(!point)return '';
            return timeText(point.value[0])+'<br>'+params.map(p=>p.marker+p.seriesName+'：'+(Number.isFinite(p.value[1])?p.value[1].toFixed(4)+'%':'--')).join('<br>')+
                '<br>报价年龄：'+ageText(point.data?.quoteAge)+'<br>'+(reference?'含缓存参考报价 · 非可成交价差':'仅新鲜双边报价 · 仍需执行前复核');
        }}}, {replaceMerge:['series','legend']});
    $('chart-note').textContent=(reference?'含缓存参考报价；缺失区间留空。':'仅保留两边 10 秒内报价；缺失区间留空。')+
        ' 路线 '+groups.size+' 条 · 样本 '+points.length+'。';
}
async function loadHistory() {
    const generation=++historyGeneration, symbol=currentSymbol, seconds=$('history-range').value;
    try {
        const pair = activeHistoryPair ? '&pair='+encodeURIComponent(activeHistoryPair) : '';
        let data;
        try {
            const response=await fetch('http://127.0.0.1:5002/api/v2/history?symbol='+encodeURIComponent(symbol)+'&seconds='+seconds+pair,{cache:'no-store'});
            if(!response.ok) throw new Error('v2 history unavailable');
            data=await response.json();
        } catch (_) {
            data=await api('/api/history/'+encodeURIComponent(symbol)+'?seconds='+seconds);
        }
        if(generation!==historyGeneration||symbol!==currentSymbol)return;
        const map=new Map(data.map(r=>[`${r.timestamp}|${r.pair_key||'legacy'}`,r]));
        rows.forEach(r=>map.set(`${r.timestamp}|${r.pair_key||'legacy'}`,r));
        rows=[...map.values()].sort((a,b)=>timestampMs(a.timestamp)-timestampMs(b.timestamp));renderChart();
    } catch(error){if(generation===historyGeneration&&symbol===currentSymbol)$('chart-note').textContent='历史读取失败：'+error.message;}
}
function sample(row) {
    const second=Math.floor(Date.now()/1000);if(second===lastHistorySample)return;lastHistorySample=second;
    if(row.history_point){
        const previous=rows.length?timestampMs(rows[rows.length-1].timestamp):null;
        if(previous!==null&&timestampMs(row.timestamp)-previous>5000)rows.push({timestamp:previous+1000,gap:true});
        rows.push(row.history_point);
    }renderChart();
}
async function loadConfig() {
    try {
        const {strategy:s}=await api('/api/config');
        const fields={'min-spread':s.spread_threshold?.min,'max-spread':s.spread_threshold?.max,'position-size':s.trading?.position_size,
            'max-positions':s.trading?.max_positions,'max-total-position-notional':s.trading?.max_total_position_notional,'leverage':s.trading?.leverage,'second-leg-retries':s.trading?.second_leg_retries,
            'slippage':s.risk?.slippage_tolerance,'execution-safety-margin':s.risk?.entry_extra_profit_bps??s.risk?.execution_safety_margin_bps??0,
            'execution-price-movement':s.risk?.execution_price_movement_bps??2,
            'book-utilization':s.risk?.book_utilization_percent??80,
            'hedge-max-delay':s.risk?.hedge_max_delay_seconds??5,
            'route-rollback-limit':s.risk?.route_rollback_limit??2,
            'route-rollback-cooldown':(s.risk?.route_rollback_cooldown_seconds??900)/60,
            'max-trades':s.wash_trading?.max_trades_per_day,
            'position-mode':s.position_policy?.mode??'single','grid-step':s.position_policy?.step_bps??5,
            'grid-max-layers':s.position_policy?.max_layers??3,'grid-layer-notional':s.position_policy?.layer_notional_usdc??12,
            'grid-total-notional':s.position_policy?.total_notional_usdc??36};
        for(const [id,value]of Object.entries(fields))if(value!==undefined)$(id).value=percentFields.has(id)?toPercent(value):value;
        window.dispatchEvent(new CustomEvent('strategy-config-loaded',{detail:s}));
        const close=s.close_policy||{};
        if(close.mode!==undefined)$('close-mode').value=close.mode;
        if(close.target_mode!==undefined)$('execution-close-target').value=close.target_mode;
        $('close-convergence-basis').value='remaining';
        if(close.convergence_bps!==undefined)$('close-convergence-bps').value=close.convergence_bps;
        if(close.min_profit_usdc!==undefined)$('close-min-profit-usdc').value=close.min_profit_usdc;
        if(close.min_profit_bps!==undefined)$('close-min-profit-bps').value=close.min_profit_bps;
        if(close.cost_reserve_usdc!==undefined)$('close-cost-reserve-usdc').value=close.cost_reserve_usdc;
        if(close.max_hold_seconds!==undefined)$('close-max-hold-minutes').value=Math.round(close.max_hold_seconds/60);
        const entry=s.entry_policy||{};
        if(entry.mode!==undefined)$('execution-entry-mode').value=entry.mode;
        if(entry.quantile!==undefined)$('execution-entry-quantile').value=entry.quantile;
        if(entry.min_samples!==undefined)$('execution-entry-min-samples').value=entry.min_samples;
        $('execution-target-window').value=entry.target_window_seconds??604800;
        $('execution-funding-mode').value=entry.funding_mode??'fixed';
        $('execution-funding-hold').value=(entry.funding_hold_seconds??3600)/60;
        $('execution-funding-extra').value=entry.funding_extra_bps??1;
        $('execution-funding-reserve').value=entry.funding_reserve_bps??
            (s.cost_model?.funding_bps_per_8h==null?'':Math.abs(s.cost_model.funding_bps_per_8h)*2*Math.max(1,Math.ceil((close.max_hold_seconds||28800)/28800)));
        const cost=s.cost_model||{};
        $('variational-fee-bps').value=toPercent(cost.variational_fee_bps??0);
        $('funding-bps').value=toPercent(cost.funding_bps_per_8h??1);
        $('variational-fee-status').value=cost.variational_fee_bps==null?'unknown':'known';
        $('funding-status').value=cost.funding_bps_per_8h==null?'unknown':'known';
        updateCostInputs();
        retainedLossLimits={max_loss_per_trade:s.risk?.max_loss_per_trade??null,max_daily_loss:s.risk?.max_daily_loss??null};
        $('loss-limits').value=Object.values(retainedLossLimits).some(x=>x!==null)?'keep':'off';
        // The first config event is emitted before the async response fields
        // are populated. Emit a second event after all strategy controls have
        // their saved values so dependent UI fields can sync immediately.
        window.StrategyChannelUI?.load(s);
        window.dispatchEvent(new CustomEvent('strategy-config-values-loaded',{detail:s}));
        await preflight();
    }catch(error){$('config-result').textContent=error.message;}
}
function updateCostInputs(){
    $('variational-fee-bps').disabled=$('variational-fee-status').value==='unknown';
    $('funding-bps').disabled=$('funding-status').value==='unknown';
}
async function preflight(){try{
    const s=await api('/api/preflight');
    $('preflight-note').textContent='配置保存不会下单。单次实盘请在“连接与实盘检查”中执行只读检查；当前缺项以该面板为准。';
    const root=$('execution-progress');root.replaceChildren();
    for(const part of s.execution_development?.components||[]){
        const title=document.createElement('h3');title.textContent=part.name;
        const done=document.createElement('p');done.className='chart-note';done.textContent='已预写：'+part.implemented;
        const pending=document.createElement('p');pending.className='chart-note';pending.textContent='待验收：'+part.pending;
        root.append(title,done,pending);
    }
}catch(e){$('preflight-note').textContent=e.message;}}
async function saveConfig(){
    const val=id=>percentFields.has(id)?toBasisPoints(Number($(id).value)):Number($(id).value);
    const saveButton=$('save-config'), executionSaveButton=$('save-execution-strategy');
    if(saveButton)saveButton.disabled=true;
    if(executionSaveButton)executionSaveButton.disabled=true;
    try {const saved=await api('/api/config',{strategy:{spread_threshold:{min:val('min-spread'),max:val('max-spread')},
        trading:{position_size:val('position-size'),max_positions:val('max-positions'),max_total_position_notional:val('max-total-position-notional'),leverage:val('leverage'),second_leg_retries:val('second-leg-retries')},
        risk:{...($('loss-limits').value==='off'?{max_loss_per_trade:null,max_daily_loss:null}:retainedLossLimits),slippage_tolerance:val('slippage'),entry_extra_profit_bps:Number($('execution-safety-margin').value),
            execution_price_movement_bps:Number($('execution-price-movement').value),
            book_utilization_percent:Number($('book-utilization').value),
            hedge_max_delay_seconds:Number($('hedge-max-delay').value),
            route_rollback_limit:Number($('route-rollback-limit').value),
            route_rollback_cooldown_seconds:Number($('route-rollback-cooldown').value)*60},
        wash_trading:{max_trades_per_day:val('max-trades')},
        position_policy:{mode:$('position-mode').value||'single',step_bps:val('grid-step'),max_layers:Number($('grid-max-layers').value),layer_notional_usdc:Number($('grid-layer-notional').value),total_notional_usdc:Number($('grid-total-notional').value)},
        ...(window.StrategyChannelUI?{channel_policy:window.StrategyChannelUI.read()}:{}),
        entry_policy:{mode:$('execution-entry-mode').value||'fixed',quantile:Number($('execution-entry-quantile').value),min_samples:Number($('execution-entry-min-samples').value),
            calculation_version:2,target_window_seconds:Number($('execution-target-window').value),
            funding_mode:$('execution-funding-mode').value,
            funding_hold_seconds:Number($('execution-funding-hold').value)*60,
            funding_extra_bps:Number($('execution-funding-extra').value),
            funding_reserve_bps:String($('execution-funding-reserve').value).trim()===''?null:Number($('execution-funding-reserve').value)},
        close_policy:{mode:$('close-mode').value||'convergence',target_mode:$('execution-close-target').value||'center',convergence_basis:$('close-convergence-basis').value||'remaining',
            convergence_bps:Number($('close-convergence-bps').value),
            min_profit_usdc:Number($('close-min-profit-usdc').value),min_profit_bps:Number($('close-min-profit-bps').value),
            cost_reserve_usdc:Number($('close-cost-reserve-usdc').value),
            max_hold_seconds:Number($('close-max-hold-minutes').value)*60},
        cost_model:{variational_fee_bps:$('variational-fee-status').value==='unknown'?null:toBasisPoints(Number($('variational-fee-bps').value)),
            funding_bps_per_8h:$('funding-status').value==='unknown'?null:toBasisPoints(Number($('funding-bps').value))}}});$('config-result').textContent=saved.message||'配置已保存到本地';window.dispatchEvent(new CustomEvent('strategy-config-saved',{detail:saved}));await loadConfig();connect();return saved;}
    catch(error){$('config-result').textContent='保存失败：'+error.message;return {success:false};}finally{if(saveButton)saveButton.disabled=false;if(executionSaveButton)executionSaveButton.disabled=false;}
}
window.saveStrategyConfig=saveConfig;
async function loadCredentials(){
    try{
        const s=await api('/api/local-credentials');
        $('credential-summary').textContent=s.saved?(s.authenticated?'只读鉴权通过 · 未启用交易':'已保存 · 本次启动未验证'):'未配置 API 私钥';
        formStatus('lighter-form-status',s.saved?(s.authenticated?'已验证':'需复查'):'未配置',s.saved&&s.authenticated);
        const lighterRow=document.querySelector('[data-platform-setting="lighter"] b');
        if(lighterRow){lighterRow.textContent=s.saved?(s.authenticated?'密钥已保存 · 只读鉴权通过':'密钥已保存 · 需要复查'):'未配置密钥';lighterRow.className=s.authenticated?'':'warn';}
        if(s.account_index)$('lighter-account').value=s.account_index;
        if(s.key_index)$('lighter-key-index').value=s.key_index;
        $('recheck-credentials').disabled=!s.saved;
        return s;
    }catch(e){$('credential-result').textContent=e.message;}
}
async function loadNetwork(){
    try{
        const data=await api('/api/network');
        $('proxy-url').value=data.proxy_url||'';
        formStatus('proxy-form-status',data.enabled?'已启用':'直连',true);
        return data;
    }catch(error){
        formStatus('proxy-form-status','读取失败',false);
        $('network-result').textContent=error.message;
        return {};
    }
}
async function saveNetwork(event){
    event.preventDefault();
    const button=$('save-network');button.disabled=true;$('network-result').textContent='正在保存并应用代理…';
    try{
        const response=await fetch('/api/network',{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json','X-Local-Token':document.querySelector('meta[name="local-token"]').content},body:JSON.stringify({proxy_url:$('proxy-url').value.trim()})});
        const data=await response.json();if(!response.ok)throw new Error(data.error||'保存失败');
        $('network-result').textContent=data.message;formStatus('proxy-form-status',data.enabled?'已启用':'直连',true);connect();
    }catch(error){$('network-result').textContent=error.message;formStatus('proxy-form-status','未应用',false);}
    finally{button.disabled=false;}
}
let credentialCheckInFlight=false;
async function saveCredentials(event){
    event.preventDefault();const button=$('save-credentials');button.disabled=true;
    $('credential-result').textContent='正在进行官方只读鉴权，最长约 45 秒；不会下单…';
    let body=JSON.stringify({account_index:Number($('lighter-account').value),key_index:Number($('lighter-key-index').value),private_key:$('lighter-private-key').value});
    $('lighter-private-key').value='';
    try{
        const pending=fetch('/api/local-credentials',{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json','X-Local-Token':document.querySelector('meta[name="local-token"]').content},body});
        body=null;const response=await pending;const data=await response.json();
        if(!response.ok){if(response.status===403&&data.error==='本机页面已失效，请刷新后重试'){window.location.reload();return;}throw new Error(data.error||'验证失败');}
        $('credential-result').textContent=data.message;await loadCredentials();
    }catch(e){$('credential-result').textContent=e.message;}
    finally{body=null;button.disabled=false;}
}
async function recheckCredentials(){
        const b=$('recheck-credentials');if(b.disabled||credentialCheckInFlight)return;credentialCheckInFlight=true;b.disabled=true;$('credential-result').textContent='正在只读复查已保存密钥与账户费率…';
        try{
            const r=await fetch('/api/local-credentials/check',{method:'POST',headers:{'Content-Type':'application/json','X-Local-Token':document.querySelector('meta[name="local-token"]').content},body:'{}'});
            const data=await r.json();if(!r.ok){if(r.status===403&&data.error==='本机页面已失效，请刷新后重试'){window.location.reload();return;}throw new Error(data.error);}$('credential-result').textContent=data.message;await loadCredentials();
        }catch(e){$('credential-result').textContent=e.message;}finally{credentialCheckInFlight=false;b.disabled=false;}
}
async function loadPlatformCredentials(){
    try{
        const all=await api('/api/platform-credentials');
        for(const venue of ['lighter_rh','standx','arcus']){
            const data=all[venue]||{}, row=document.querySelector('[data-platform-setting="'+venue+'"] b');
            if(!row)continue;
            const checkedAt=Date.parse(data.verification?.checked_at||'');
            const verified=(data.authenticated===true||data.verification?.authenticated===true) && Number.isFinite(checkedAt) && Date.now()-checkedAt<=300000;
            const statusId=venue==='lighter_rh'?'lighter-rh-form-status':venue==='standx'?'standx-form-status':'arcus-form-status';
            formStatus(statusId,data.saved?(verified?'已验证':'需复查'):'未配置',data.saved&&verified);
            const registeredIndex=data.verification?.registered_account_index;
            const binding=venue==='arcus'&&data.verification?.api_key_name&&registeredIndex!==null&&registeredIndex!==undefined
                ? ` · ${data.verification.api_key_name} · Subaccount #${registeredIndex}` : '';
            row.textContent=data.saved?(verified?`已保存 · 只读鉴权通过${binding}`:'已保存 · 需要复查'):'未配置';
            row.className=verified?'':'warn';
            const resultNode=$(venue==='lighter_rh'?'lighter-rh-credential-result':venue==='standx'?'standx-credential-result':'arcus-credential-result');
            if(resultNode&&data.verification?.message){
                const savedNote=venue==='arcus'&&data.saved?'；API Signing Key 已保存到本机，后续复查无需重填':'';
                resultNode.textContent=data.verification.message+savedNote;
            }
            if(venue==='lighter_rh'){
                if(data.account_index)$('lighter-rh-account').value=data.account_index;
                if(data.key_index)$('lighter-rh-key-index').value=data.key_index;
            }else if(venue==='standx'&&data.wallet_address){$('standx-address').value=data.wallet_address;
            }else if(venue==='arcus'){
                if(data.wallet_address)$('arcus-address').value=data.wallet_address;
                if(data.account_index!==null&&data.account_index!==undefined)$('arcus-account').value=data.account_index;
            }
        }
        return all;
    }catch(error){
        for(const id of ['lighter-rh-credential-result','standx-credential-result'])if($(id))$(id).textContent=error.message;
        return {};
    }
}
async function recheckPlatformCredentials(venue){
    const prefix=venue==='lighter_rh'?'lighter-rh':venue;
    const button=$('recheck-'+prefix+'-credentials');
    const result=$(prefix+'-credential-result');
    if(!button||button.disabled)return;
    button.disabled=true;result.textContent='正在复查已保存密钥，只读不下单…';
    try{
        const response=await fetch('/api/platform-credentials/check',{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json','X-Local-Token':document.querySelector('meta[name="local-token"]').content},body:JSON.stringify({venue})});
        const data=await response.json();if(!response.ok)throw new Error(data.error||'复查失败');
        result.textContent=data.message;await loadPlatformCredentials();
    }catch(error){result.textContent=error.message;}
    finally{button.disabled=false;}
}
async function savePlatformCredentials(venue, event){
    event.preventDefault();
    const prefix=venue==='lighter_rh'?'lighter-rh':venue;
    const button=$('save-'+prefix+'-credentials');
    const result=$(prefix+'-credential-result');
    button.disabled=true;result.textContent='正在进行官方只读鉴权，不会下单…';
    const payload=venue==='standx'?{
        venue, wallet_address:$('standx-address').value.trim(),
        wallet_private_key:$('standx-private-key').value, chain:$('standx-chain').value
    }:venue==='arcus'?{venue, wallet_address:$('arcus-address').value.trim(),
        account_index:Number($('arcus-account').value), api_signing_key:$('arcus-signing-key').value
    }:{venue, account_index:Number($('lighter-rh-account').value),
        key_index:Number($('lighter-rh-key-index').value), private_key:$('lighter-rh-private-key').value};
    try{
        const response=await fetch('/api/platform-credentials',{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json','X-Local-Token':document.querySelector('meta[name="local-token"]').content},body:JSON.stringify(payload)});
        const data=await response.json();if(!response.ok)throw new Error(data.error||'验证失败');
        result.textContent=data.message;
        // Clear secrets only after a successful verification and durable save.
        // On failure, keep the input so the user can correct the value without
        // having to paste it again.
        if(venue==='standx')$('standx-private-key').value='';else if(venue==='arcus')$('arcus-signing-key').value='';else $('lighter-rh-private-key').value='';
        await loadPlatformCredentials();
    }catch(error){result.textContent=error.message;}
    finally{button.disabled=false;}
}
document.addEventListener('DOMContentLoaded',()=>{
    const settingsDialog=$('settings-dialog'),settingsBody=$('settings-dialog-body');
    ['strategy-settings','connection-settings'].forEach(id=>settingsBody.appendChild($(id)));
    $('settings-open').onclick=()=>{
        if(typeof settingsDialog.showModal==='function')settingsDialog.showModal();
        else settingsDialog.setAttribute('open','');
    };
    $('settings-close').onclick=()=>settingsDialog.close?.();
    settingsDialog.addEventListener('click',event=>{if(event.target===settingsDialog)settingsDialog.close?.();});
    $('readiness-open').onclick=()=>{
        const panel=$('desk-run');
        const controls=$('execution-controls');
        if(controls) controls.open=true;
        panel?.scrollIntoView({block:'start'});
        $('execution-check')?.focus();
    };
    initChart();connect();loadConfig();loadExecutionHistory();$('history-refresh').onclick=loadExecutionHistory;
    loadNetwork();loadCredentials();loadPlatformCredentials();$('network-form').onsubmit=saveNetwork;$('credentials-form').onsubmit=saveCredentials;
    $('lighter-rh-credentials-form').onsubmit=event=>savePlatformCredentials('lighter_rh',event);
    $('standx-credentials-form').onsubmit=event=>savePlatformCredentials('standx',event);
    $('arcus-credentials-form').onsubmit=event=>savePlatformCredentials('arcus',event);
    $('recheck-lighter-rh-credentials').onclick=()=>recheckPlatformCredentials('lighter_rh');
    $('recheck-standx-credentials').onclick=()=>recheckPlatformCredentials('standx');
    $('recheck-arcus-credentials').onclick=()=>recheckPlatformCredentials('arcus');
    $('recheck-credentials').onclick=recheckCredentials;
    // The server refreshes saved credentials and fees every four minutes.
    // Multiple open tabs must not start competing verification requests.
    $('market-select').onchange=connect;$('notional').onchange=connect;$('refresh-btn').onclick=connect;
    $('chart-mode').onchange=renderChart;
    $('history-range').onchange=()=>{rows=[];loadHistory();};$('save-config').onclick=saveConfig;
    $('variational-fee-status').onchange=updateCostInputs;$('funding-status').onchange=updateCostInputs;
    $('pair-btn').onclick=async()=>{try{const data=await api('/api/bridge/pair',{});$('pair-token').hidden=false;$('pair-token').value=data.token;$('pair-token').select();}catch(e){$('pair-btn').textContent=e.message;}};
    setInterval(()=>{if(lastEvent&&Date.now()-lastEvent>6000&&!disconnected){disconnected=true;clearLive('数据流超过 6 秒未更新，旧价格已隐藏，正在重连。');pill('stream-status','数据流中断','warn');}},1000);
    window.addEventListener('beforeunload',()=>events?.close());
    setInterval(()=>{if(!document.hidden)loadExecutionHistory();},2000);
});
