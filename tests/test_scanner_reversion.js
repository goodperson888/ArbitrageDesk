'use strict';
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
let now = 100000, fail = false;
const calls = [], nodes = new Map();
function element() {
    return {children: [], dataset: {}, classList: {toggle() {}},
        appendChild(child) {this.children.push(child);}, append() {},
        replaceChildren() {this.children = [];}, setAttribute() {}, addEventListener() {}};
}
for (const id of ['v2-opportunity-result', 'v2-scan-coverage']) nodes.set(id, element());
const context = vm.createContext({
    window: {QuoteFreshness: {describe: row => ({stale: !!row.buy_stale || !!row.sell_stale})}},
    document: {getElementById: id => nodes.get(id), createElement: element,
        addEventListener() {}, querySelectorAll: () => []},
    localStorage: {getItem: () => '{}'},
    Date: class extends Date {static now() {return now;}},
    AbortController, setTimeout, clearTimeout, console,
    fetch: async url => {
        const query = new URL(url).searchParams;
        const items = query.get('items').split(','); calls.push(items);
        if (fail) throw new Error('test timeout');
        return {ok: true, json: async () => ({items: items.map(item => {
            const [symbol, pair_key] = item.split('|');
            return {symbol: symbol + '-PERP', pair_key, samples: 10,
                reversion_count: 2, last_reversion_age_seconds: 10};
        })})};
    }
});
const source = fs.readFileSync('src/web/static/js/cross_venue.js', 'utf8');
vm.runInContext(source.replace(/\}\)\(\);\s*$/, 'window.test = {state, reversionKey, reversionAllowed, ensureReversionStats, renderResult, reversionEstimate, reversionEstimateDetail, opportunityRank, refreshReversionEstimate, updateFreshness};})();'), context);
const {state, reversionKey, reversionAllowed, ensureReversionStats, renderResult} = context.window.test;
const route = (symbol, reverse = false, net = 1) => ({canonical_symbol: symbol + '-PERP',
    buy_venue: reverse ? 'arcus' : 'lighter', sell_venue: reverse ? 'lighter' : 'arcus',
    net_bps: net, gross_bps: net, buy_price: 1, sell_price: 1.01});
const stats = (row, count = 2) => state.reversionStats.set(reversionKey(row), {
    samples: 10, reversion_count: count, last_reversion_age_seconds: 10, _loaded_at: now});
const settle = () => new Promise(resolve => setImmediate(resolve));
(async () => {
    const older = route('OLDER'), recent = route('RECENT');
    state.reversionMinCount = 1; state.reversionMinSamples = 1; state.reversionMaxAge = 3600;
    stats(older); stats(recent);
    state.reversionStats.get(reversionKey(older)).last_reversion_age_seconds = 4000;
    assert.equal(state.reversionRecencyMode, 'hard', 'existing preferences retain hard filtering');
    assert.equal(reversionAllowed(older), false);
    state.reversionRecencyMode = 'soft';
    assert.equal(reversionAllowed(older), true, 'old recency is allowed to undergo profit evaluation');
    state.reversionStats.get(reversionKey(older)).statistics_ready = false;
    assert.equal(reversionAllowed(older), false, 'pending or expired shared statistics cannot approve entry');
    state.reversionStats.get(reversionKey(older)).statistics_ready = true;
    assert(context.window.test.opportunityRank(recent)[2] > context.window.test.opportunityRank(older)[2]);
    assert(context.window.test.opportunityRank({...older, net_bps: 2})[1] > context.window.test.opportunityRank(recent)[1], 'profit remains primary');
    state.reversionStats.get(reversionKey(older)).samples = 0;
    assert.equal(reversionAllowed(older), false, 'soft recency cannot bypass required samples');
    assert.equal(context.window.getExecutionSelection().reversion_recency_mode, 'soft');
    state.reversionRecencyMode = 'hard'; state.reversionMinCount = 0; state.reversionMinSamples = 0; state.reversionMaxAge = 0;
    state.reversionStats.clear();
    const preview = (profit=.024,net=20) => ({version:1,requested_target_mode:'center',
        window_seconds:604800,target_mode:'center',target_bps:-40,profit_usdc:profit,net_bps:net,
        quantity:.12,reference_notional:12,minimum_bps:8,gap_bps:0,meets_floor:net>=8,
        costs:{funding_bps:5},reason:'估算达到收益门槛；数量、账户及最新报价待核验'});
    const estimateRow = {...route('ESTIMATE', false, -15), reversion_ready:true,
        estimated_reversion_profit_usdc:.024, history_center_bps:-40,
        favorable_deviation_bps:30, history_7d_samples:100,return_preview:preview()};
    assert.match(context.window.test.reversionEstimate(estimateRow), /0\.0240 U/,
        'negative opening net can still have a positive complete round trip');
    assert.match(context.window.test.reversionEstimate({...estimateRow,
        estimated_reversion_profit_usdc:-.024,return_preview:preview(-.024,-20)}), /-0\.0240 U/);
    assert.equal(context.window.test.reversionEstimate({...estimateRow,return_preview:{}}), '—');
    assert.match(context.window.test.reversionEstimateDetail(estimateRow), /资金费预算 5.00 bps/);
    const detailed = {...estimateRow,return_preview:{...preview(.0072,6),cost_model_version:2,
        gross_bps:30,entry_reserve_bps:3,movement_bps:2,
        components:{target_remaining_bps:8,close_quote_width_bps:2,fees_bps:9,
            book_impact_bps:1,movement_budget_bps:4,funding_cost_bps:0,funding_extra_bps:0},
        movement_evidence:{samples:1,movement_bps:0},reason:'整轮净收益未达入场门槛'}};
    let calculation=context.window.test.reversionEstimateDetail(detailed);
    assert.match(calculation,/30\.0000 − 8\.0000 − 2\.0000 − 9\.0000 − 1\.0000 − 4\.0000 − 0\.0000 − 0\.0000 = 6\.0000 bps/);
    assert.match(calculation,/5\.0000（有效净收益底线） \+ 3\.0000（入场预留） = 8\.0000 bps/);
    assert.match(calculation,/6\.0000 ÷ 10000 × 12\.000000 U = 0\.007200 U/);
    const observedOnly={...detailed,return_preview:{...detailed.return_preview,
        movement_evidence:{samples:6,movement_bps:35,projection_decay_bps:40,observation_only:true}}};
    const observedText=context.window.test.reversionEstimateDetail(observedOnly);
    assert.match(observedText,/历史窗口仅观察，不加预算/);
    assert.doesNotMatch(observedText,/取大值|P90 校准/);
    assert.match(calculation,/预计两笔平仓价格变动预算 4\.00 bps/);
    assert.match(calculation,/开仓固定变价扣减为0/);
    assert.match(calculation,/样本不足5次/);
    assert.doesNotMatch(calculation,/四次成交价格变动预算|NaN|undefined/);
    renderResult([detailed]);
    const netCell=nodes.get('v2-opportunity-result').children[0].children.find(cell=>cell.className.startsWith('reverse-net'));
    assert.match(netCell.title,/代入（bps）：30\.0000/,'the actual hover target contains the current numerical equation');
    calculation=context.window.test.reversionEstimateDetail({...detailed,return_preview:{...detailed.return_preview,
        net_bps:22,components:{...detailed.return_preview.components,target_remaining_bps:-8}}});
    assert.match(calculation,/30\.0000 \+ 8\.0000 − 2\.0000/,'negative residual targets contribute with the correct sign');
    calculation=context.window.test.reversionEstimateDetail({...detailed,return_preview:{...detailed.return_preview,
        components:{...detailed.return_preview.components,fees_bps:null}}});
    assert.match(calculation,/完整数值算式待同轮报价补齐/);
    assert.match(calculation,/开平仓手续费 未知/,'a missing fee is not presented as zero in the equation');
    assert.equal(context.window.test.opportunityRank({...estimateRow,net_bps:null,
        return_preview:{},estimated_reversion_profit_usdc:null})[0],0);
    const recalc = context.window.test.refreshReversionEstimate;
    const newTick = recalc({...estimateRow,return_preview:preview(.03,25)});
    assert.equal(newTick.estimated_reversion_profit_usdc,.03,'use the new tick complete model result');
    assert.equal(recalc({...newTick,return_preview:{}}).estimated_reversion_profit_usdc,null,
        'an old history center cannot resurrect the previous tick profit');
    state.reversionTargetMode='zero';
    assert.equal(recalc(newTick).estimated_reversion_profit_usdc,null,'target change invalidates immediately');
    state.reversionTargetMode='center';state.reversionWindow=86400;
    assert.equal(recalc(newTick).estimated_reversion_profit_usdc,null,'window change invalidates immediately');
    state.reversionWindow=604800;
    const channelTick={...newTick,return_preview:{...preview(.03,25),target_mode:'channel',window_seconds:900,channel_policy:{method:'fixed'}}};
    context.window.StrategyChannelUI={basis:()=>({mode:'channel',policy:{method:'fixed'}})};
    assert.equal(recalc(channelTick).estimated_reversion_profit_usdc,.03,'channel estimates follow the saved channel window independently of history filters');
    context.window.StrategyChannelUI.basis=()=>({mode:'channel',policy:{method:'robust'}});
    assert.equal(recalc(channelTick).estimated_reversion_profit_usdc,null,'saving a different channel rejects old policy estimates');
    delete context.window.StrategyChannelUI;
    assert.equal(recalc({...newTick,buy_age_seconds:5.1}).estimated_reversion_profit_usdc,null);
    assert.equal(recalc({...newTick,sell_stale:true}).return_preview.net_bps,null);
    renderResult([{...newTick,history_samples:100}]);
    assert.equal(state.rows[0].estimated_reversion_profit_usdc,.03);
    renderResult([{...route('ESTIMATE',false,5),return_preview:preview(.04,33)}]);
    assert.equal(state.rows[0].estimated_reversion_profit_usdc,.04);
    renderResult([{...route('ESTIMATE',false,5)}]);
    assert.equal(state.rows[0].estimated_reversion_profit_usdc,null,
        'stream rows without current estimate cannot inherit an old one');
    // The per-second age updater must preserve return blockers and clear
    // estimates at the stricter 5s execution-estimate boundary.
    const cells = new Map([['.age',{}],['.status',{}],['.net',{}],['.reversion-score',{}]]);
    const card = {quoteRow:newTick,classList:{toggle(){}},querySelector:selector=>cells.get(selector)};
    context.document.querySelectorAll = selector => selector === '#v2-opportunity-result .v2-opportunity-row' ? [card] : [];
    context.window.test.updateFreshness();
    assert.match(cells.get('.status').textContent,/收益达标.*待实盘复核/);
    card.quoteRow={...newTick,buy_age_seconds:6};
    context.window.test.updateFreshness();
    assert.match(cells.get('.status').textContent,/报价超过实盘估算时效/);
    assert.equal(cells.get('.net').textContent,'—');
    assert.equal(cells.get('.reversion-score').textContent,'—');
    context.document.querySelectorAll=()=>[];
    renderResult([{...route('WEAR'),wear_bps:null,wear_breakdown:{fees_bps:4.5}}]);
    let wearCell = nodes.get('v2-opportunity-result').children[0].children.find(cell=>cell.className==='wear');
    assert.equal(wearCell.textContent,'已知手续费 4.50 bps');
    assert.equal(wearCell.children[0].textContent,'滑点待测 · 合计未齐');
    renderResult([{...route('WEAR'),wear_bps:4.5,wear_breakdown:{fees_bps:4.5,slippage_bps:0}}]);
    wearCell = nodes.get('v2-opportunity-result').children[0].children.find(cell=>cell.className==='wear');
    assert.match(wearCell.textContent,/^入场 /);
    assert.equal(wearCell.children[0].textContent,'手续费 + 盘口冲击（估算）');
    renderResult([{...route('WEAR'),wear_bps:null,wear_breakdown:{fees_bps:null}}]);
    wearCell = nodes.get('v2-opportunity-result').children[0].children.find(cell=>cell.className==='wear');
    assert.equal(wearCell.textContent,'手续费待核验','unknown fee must not appear as zero');
    const ab={...route('PAIR',true),return_preview:preview(.02,16)}, ba={...route('PAIR'),return_preview:preview(.03,25)};
    renderResult([ab,ba]);
    let paired=state.rows[0];
    assert.deepEqual(Array.from(paired.pair_axis),['arcus','lighter']);
    assert.equal(paired.buy_venue,'lighter','preferred direction follows the higher round-trip profit');
    assert.equal(paired.pair_directions.forward.return_preview.net_bps,16);
    assert.equal(paired.pair_directions.reverse.return_preview.net_bps,25,'reverse net is not the negative of forward');
    renderResult([{...ab,return_preview:preview(.04,33)},ba]);
    paired=state.rows[0];
    assert.equal(paired.buy_venue,'arcus');
    assert.deepEqual(Array.from(paired.pair_axis),['arcus','lighter'],'A/B never flip with the winning direction');
    renderResult([ab]);
    assert.equal(state.rows[0].pair_directions.reverse,undefined,'missing direction stays unknown');
    let reverseCell=nodes.get('v2-opportunity-result').children[0].children.find(cell=>cell.className.startsWith('reverse-net'));
    assert.equal(reverseCell.textContent,'—');
    assert.match(reverseCell.title,/不会使用另一方向的相反数/);
    const untouched={...ba,return_preview:{...preview(.2,160),meets_floor:false,reason:'未触边'}};
    renderResult([ab,untouched]);
    assert.equal(state.rows[0].buy_venue,'arcus','boundary-ready route wins over a more profitable untouched scenario');
    const q1={...ab,buy_age_seconds:1,sell_age_seconds:1}, q2={...ba,return_preview:preview(.02,16),buy_age_seconds:2,sell_age_seconds:2};
    renderResult([q2,q1]);
    assert.equal(state.rows[0].buy_venue,'arcus','equal profit is ranked by quote freshness');
    state.spreadMinBps=100;
    renderResult([{...ab,buy_price:100,sell_price:101,gross_bps:100},
        {...ba,buy_price:102,sell_price:100,gross_bps:-196}]);
    assert.equal(state.rows[0].buy_venue,'lighter','gross filtering uses the same max-price denominator as execution');
    assert.match(state.rows[0].pair_directions.forward.direction_filter_reason,/毛价差筛选/,
        'the filtered counterpart is retained for explanation');
    state.spreadMinBps=0;
    // Execution scope is the full current catalog, independent of displayed
    // candidates, fresh quotes, or whether history has arrived yet.
    const instruments = Array.from({length:60},(_,i)=>({canonical_symbol:'M'+i+'-PERP'}));
    state.selectedVenues=['lighter','arcus'];
    state.instruments={lighter:instruments,arcus:instruments};
    state.excludedSymbols=['M2'];state.symbolMode='all';
    state.liveSymbols=['STALE'];state.rows=[route('M0')];
    state.reversionMinCount = 1; state.reversionMaxAge = 3600;
    let selection=context.window.getExecutionSelection();
    assert.equal(selection.symbols.length,59,'all eligible catalog symbols are sent even with one visible candidate');
    assert(selection.symbols.includes('M59'),'initially filtered-out symbols remain monitored');
    assert(!selection.symbols.includes('M2'),'excluded symbols remain outside execution');
    assert(!selection.symbols.includes('STALE'),'stale scan snapshots cannot define the new scope');
    state.rows=[];
    assert.equal(context.window.getExecutionSelection().symbols.length,59,'no current candidates does not empty the execution scope');
    state.symbolMode='selected';state.selectedSymbols=['M3'];
    assert.deepEqual(Array.from(context.window.getExecutionSelection().symbols),['M3'],'selected mode must preserve the selected symbol while history is unavailable');
    state.excludedSymbols=['M3'];
    assert.equal(context.window.getExecutionSelection().symbols.length,0,'selected mode also respects exclusions');
    state.symbolMode='all';state.selectedVenues=['lighter','lighter_rh'];
    assert.equal(context.window.getExecutionSelection().symbols.length,0,'missing new-pair catalog cannot reuse old live symbols');
    state.selectedVenues=['lighter','arcus'];state.excludedSymbols=[];
    const rows = Array.from({length: 60}, (_, i) => [route('M' + i), route('M' + i, true)]).flat();
    state.allRows = rows; state.liveSymbols = rows.filter((_, i) => i % 2 === 0).map(x => x.canonical_symbol);
    await ensureReversionStats(rows); await settle(); await settle();
    assert.deepEqual(calls.map(x => x.length), [80, 40]);
    assert.equal(state.reversionStats.size, 120, 'all symbols and both routes receive statistics');
    await ensureReversionStats(rows); assert.equal(calls.length, 2, 'live ticks reuse fresh stats');
    now += 30001; await ensureReversionStats(rows); await settle(); await settle();
    assert.equal(calls.length, 4, 'cached historical filters refresh without reloading the page');
    const forward = route('REVERSE', false, 20), reverse = route('REVERSE', true, 10);
    stats(forward, 0); stats(reverse); state.allRows = [forward, reverse];
    renderResult(state.allRows);
    assert.equal(state.rows.length, 1); assert.equal(state.rows[0].buy_venue, 'arcus',
        'a passing reverse route survives even when the higher-ranked forward route fails history');
    assert.match(nodes.get('v2-scan-coverage').textContent, /筛选后 1 条路线/);
    stats(reverse); now += 3600001;
    assert.equal(reversionAllowed(reverse), false, 'last reversion age advances between responses');
    fail = true; state.allRows = [route('FAIL')]; state.reversionStats.clear();
    await ensureReversionStats(state.allRows); await settle();
    const failures = calls.length; await ensureReversionStats(state.allRows);
    assert.equal(calls.length, failures, 'failed stats requests back off instead of creating a retry loop');
    console.log('Scanner reversion refresh, full two-way coverage, route filtering, age and retry backoff: PASS');
})().catch(error => {console.error(error); process.exitCode = 1;});
