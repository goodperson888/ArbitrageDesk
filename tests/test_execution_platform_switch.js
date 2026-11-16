'use strict';
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const nodes = new Map(), events = new Map(), requests = [];
let init, selected = ['lighter', 'lighter_rh'], execution = {running:false, state:'idle'}, releaseCheck;
let selection = {mode:'selected',symbols:['ETH']}, userScope = null, bestPair = null;
let scopeResponse = null, releaseScope = null;
const node = id => {
    if (!nodes.has(id)) nodes.set(id, {id, value:'12', textContent:'', disabled:false, checked:false,
        setAttribute(){}, before(child){nodes.set(child.id,child);}, replaceChildren(){this.children=[];},
        appendChild(child){(this.children ||= []).push(child);}});
    return nodes.get(id);
};
const operations = () => ({execution, lighter:{}, bridge:{symbols:[]},
    feeds:{variational:{},lighter:{}}, capture:{position:{known:false}, chains:[], warnings:[], evidence:[]}, diagnostics:[]});
const context = vm.createContext({
    document:{getElementById:node, createElement:()=>({setAttribute(){}}), querySelector:()=>({content:'test'}),
        addEventListener:(_,fn)=>{init=fn;}, hidden:false},
    getSelectedVenues:()=>selected.slice(), getExecutionSelection:()=>({...selection,symbols:selection.symbols.slice()}),
    getExecutionScope:()=>userScope || selection,
    getBestExecutionVenues:()=>bestPair,
    addEventListener:(name,fn)=>events.set(name,fn), Date, AbortController, setTimeout, clearTimeout,
    setInterval(){}, URLSearchParams, console,
    fetch:async(path, options)=>{
        requests.push({path, payload:options?.body ? JSON.parse(options.body) : null});
        if(path.endsWith('/check')) {
            const data = releaseCheck === 'defer' ? await new Promise(resolve=>{releaseCheck=resolve;}) : {ready:true};
            return {ok:true,json:async()=>data};
        }
        if(path.endsWith('/reset-stop')) return {ok:true,json:async()=>({success:true,orders_submitted:false})};
        if(path.endsWith('/scope')) {
            const data = scopeResponse === 'defer' ? await new Promise(resolve=>{releaseScope=resolve;})
                : scopeResponse || {success:true,request_id:'scope-1',message:'正在后台核验新仓平台'};
            return {ok:true,json:async()=>data};
        }
        return {ok:true,json:async()=>operations()};
    }
});
vm.runInContext(fs.readFileSync('src/web/static/js/operations.js','utf8'),context);
const tick = () => new Promise(resolve=>setImmediate(resolve));
(async()=>{
    node('execution-trade-mode').value='close_only'; // Restored browser form state is not launch intent.
    init(); await tick();
    assert.equal(node('execution-trade-mode').value,'open_close');
    node('execution-trade-mode').value='close_only'; // No user change event.
    await node('execution-check').onclick();
    assert.equal(requests.filter(r=>r.path.endsWith('/check')).at(-1).payload.close_only,false);
    assert.equal(node('live-arm').disabled,false);
    node('execution-trade-mode').value='close_only';node('execution-trade-mode').onchange();
    assert.equal(node('live-arm').disabled,true,'mode edits invalidate the previous check');
    await node('execution-check').onclick();
    assert.equal(requests.filter(r=>r.path.endsWith('/check')).at(-1).payload.close_only,true);
    assert.equal(node('execution-once').disabled,true,'close-only mode must not offer an opening validation');
    const beforeSingle=requests.length;
    await node('execution-once').onclick();
    assert.equal(requests.length,beforeSingle,'close-only single validation must never submit a request');
    node('execution-trade-mode').value='open_close';node('execution-trade-mode').onchange();
    await node('execution-check').onclick();
    selected=['lighter','arcus']; events.get('execution-selection-changed')(); await tick();
    assert.equal(node('live-arm').disabled,true,'venue change must invalidate the previous check');
    await node('execution-once').onclick();
    assert.equal(requests.some(r=>/\/(once|wait-once|monitor)$/.test(r.path)),false);

    releaseCheck='defer'; const checking=node('execution-check').onclick(); await tick();
    selected=['lighter','lighter_rh']; events.get('execution-selection-changed')();
    releaseCheck({ready:true}); releaseCheck=null; await checking;
    assert.equal(node('live-arm').disabled,true,'a late response for the old selection must stay locked');
    assert.match(node('execution-check-result').textContent,/修改前组合/);

    // All-symbol candidates come from live quotes, not user settings. Both
    // reordering and membership changes during a slow check are normal.
    userScope={mode:'all',symbols:[],excluded_symbols:['BTC']};
    selection={mode:'all',symbols:['BABA','ENA','CRCL']};
    for(const symbols of [['CRCL','ENA','BABA'], ['UNI','ENA']]) {
        releaseCheck='defer'; const check=node('execution-check').onclick(); await tick();
        selection={...selection,symbols};
        releaseCheck({ready:true}); releaseCheck=null; await check;
        assert.equal(node('live-arm').disabled,false,'live candidate changes must not invalidate user scope');
        assert.doesNotMatch(node('execution-arm-status').textContent,/已修改|失效/);
    }
    userScope={mode:'selected',symbols:['BABA','ENA'],excluded_symbols:[]};
    selection={mode:'selected',symbols:['BABA','ENA']};
    releaseCheck='defer'; const sortedCheck=node('execution-check').onclick(); await tick();
    userScope={...userScope,symbols:['ENA','BABA','ENA']};
    selection={...selection,symbols:['ENA']};
    releaseCheck({ready:true}); releaseCheck=null; await sortedCheck;
    assert.equal(node('live-arm').disabled,false,'selected symbol scope is a set, independent of live filtering');

    // Genuine edits must still reject a late response, even without a UI event.
    const edits=[()=>{userScope={...userScope,symbols:['UNI']};},
        ()=>{userScope={...userScope,excluded_symbols:['ENA']};},
        ()=>{userScope={...userScope,mode:'all'};},
        ()=>{selection={...selection,gross_spread_min_bps:20};},
        ()=>{node('execution-cap').value='11';}];
    for(const edit of edits){
        userScope={mode:'selected',symbols:['ENA','BABA'],excluded_symbols:[]};
        selection={mode:'selected',symbols:['ENA','BABA']}; node('execution-cap').value='12';
        releaseCheck='defer'; const check=node('execution-check').onclick(); await tick();
        edit(); releaseCheck({ready:true}); releaseCheck=null; await check;
        assert.equal(node('live-arm').disabled,true,'real settings edits must invalidate the check');
        assert.match(node('execution-check-result').textContent,/修改前组合/);
    }
    userScope={mode:'all',symbols:[],excluded_symbols:[]};selection={mode:'all',symbols:['ENA']};
    releaseCheck='defer'; const blocked=node('execution-check').onclick(); await tick();
    selection={...selection,symbols:['BABA']};
    releaseCheck({ready:false,blockers:['停止标志存在']}); releaseCheck=null; await blocked;
    assert.match(node('execution-check-result').textContent,/停止标志存在/,'server blockers must remain visible');
    assert.doesNotMatch(node('execution-arm-status').textContent,/检查期间/);

    // A three-platform ranking can change repeatedly during verification.
    // Preserve the backend-approved pair for launch rather than invalidating
    // the user's unchanged scope or substituting a newly ranked pair.
    selected=['lighter','lighter_rh','arcus']; bestPair=['lighter','lighter_rh'];
    releaseCheck='defer'; const multiCheck=node('execution-check').onclick(); await tick();
    bestPair=['lighter_rh','arcus'];
    releaseCheck({ready:true,venues:['lighter','arcus']}); releaseCheck=null; await multiCheck;
    assert.equal(node('live-arm').disabled,false,'ranking changes cannot invalidate a three-platform check');
    assert.doesNotMatch(node('execution-check-result').textContent,/修改前组合/);
    bestPair=['lighter','lighter_rh'];
    node('live-arm').onclick(); await tick();
    assert.deepEqual(requests.filter(r=>r.path.endsWith('/monitor')).at(-1).payload.venues,
        ['lighter','arcus'],'a simulated launch uses the pair returned by the check');
    await node('execution-check').onclick();
    assert.equal(node('live-arm').disabled,false);
    selected=['lighter','arcus']; events.get('execution-selection-changed')(); await tick();
    assert.equal(node('live-arm').disabled,true,'actual platform edits still invalidate authorization');
    bestPair=null;
    userScope=null;selection={mode:'selected',symbols:['ETH']};node('execution-cap').value='12';

    selected=['lighter','arcus'];
    execution={running:true,state:'running',mode:'continuous_monitor',venues:['lighter','lighter_rh'],
        symbols:['BABA'],latest_event:{event:'stage_completed',detail:{stage:'Lighter RH 参考价格'}}};
    await node('ops-refresh').onclick();
    assert.match(node('execution-live-title').textContent,/实盘仍运行旧组合/);
    assert.match(node('execution-platform-state').textContent,/正在执行：Lighter 主网 ↔ Lighter RH/);
    assert.match(node('execution-platform-state').textContent,/当前行情选择：Lighter 主网 ↔ Arcus/);
    assert.match(node('execution-auto-state').textContent,/BABA/);
    assert.equal(node('execution-stop').hidden,true,'continuous mode uses its main stop button only');
    assert.equal(node('execution-check').disabled,true);
    const count=requests.length; await node('execution-check').onclick();
    assert.equal(requests.length,count,'running worker must block a new check/start');

    selected=['lighter_rh','lighter']; events.get('execution-selection-changed')(); await tick();
    assert.equal(node('live-arm').disabled,false,'changing selection must keep the running worker stop control available');
    assert.match(node('execution-live-title').textContent,/全局实盘等待中/);
    assert.doesNotMatch(node('execution-live-title').textContent,/单次验证|旧组合/);
    execution={...execution,latest_event:{event:'stage_completed',detail:{stage:'Arcus 参考价格'}},
        reference_blocker:{event:'stage_failed',timestamp:new Date().toISOString(),
            detail:{symbol:'BOT',stage:'Lighter 主网 参考价格',reason:'行情请求失败: HTTP 405'}}};
    await node('ops-refresh').onclick();
    assert.match(node('execution-live-title').textContent,/实盘报价受阻.*BOT.*Lighter 主网/);
    assert.match(node('execution-live-detail').textContent,/HTTP 405/);
    execution.reference_blocker=null;
    execution.last_rejection={event:'stage_failed',detail:{stage:'Lighter 主网 参考价格',reason:'行情请求失败: HTTP 405'}};
    await node('ops-refresh').onclick();
    assert.doesNotMatch(node('execution-live-title').textContent,/实盘报价受阻/);
    assert.doesNotMatch(node('execution-live-detail').textContent,/HTTP 405|链路正常/);
    selected=['lighter','arcus'];
    execution={...execution,venues:['lighter','arcus'],
        last_rejection:{id:10,event:'blocked',detail:{symbol:'MRVL',execution_id:'a',reason:'没有符合净价差与金额上限的深度'}},
        last_quote_evaluation:{id:14,event:'quote_evaluation',detail:{symbol:'BOT',execution_id:'b',net_edge_bps:99}},
        rejection_quote_evaluation:{id:9,event:'quote_evaluation',detail:{symbol:'MRVL',execution_id:'a',direction:'sell_variational',
            discovery_gross_bps:15,gross_edge_bps:6.39,net_edge_bps:-2.10,min_edge_bps:5}}};
    await node('ops-refresh').onclick();
    assert.match(node('execution-live-detail').title,/MRVL.*买 Arcus \/ 卖 Lighter 主网/);
    assert.match(node('execution-live-detail').title,/初筛参考毛价差 15.00.*报价毛价差 6.39.*保护后净价差 -2.10/);
    assert.match(node('execution-live-detail').title,/门槛 5 bps/);
    assert.match(node('execution-live-detail').textContent,/MRVL.*买 Arcus.*保护后净价差 -2.10 \/ 门槛 5 bps/);
    assert.doesNotMatch(node('execution-live-detail').title,/BOT|99.00|最近复核/);
    execution.rejection_quote_evaluation=execution.last_quote_evaluation;
    await node('ops-refresh').onclick();
    assert.doesNotMatch(node('execution-live-detail').title,/BOT|99.00|该次检查较优方向/);
    selected=['lighter','arcus'];
    execution={...execution,venues:selected.slice(),managed_positions:[{symbol:'PUMP'}],
        latest_event:{event:'position_resumed',timestamp:new Date().toISOString(),detail:{symbol:'PUMP'}}};
    await node('ops-refresh').onclick();
    assert.match(node('execution-live-title').textContent,/原仓位已接管.*持仓监控中/);
    assert.match(node('execution-live-detail').textContent,/PUMP.*沿用原成交记录/);
    assert.match(node('execution-mode').textContent,/原仓位已接管/);
    execution={...execution,latest_event:{event:'position_reconcile_failed',timestamp:new Date(Date.now()-10000).toISOString(),detail:{}},
        position_monitor:{heartbeat_stale:false,updated_at:new Date().toISOString(),positions:[{account_status:'ready',last_success_at:new Date().toISOString(),error:'持仓报价待更新'}]}};
    await node('ops-refresh').onclick();
    assert.match(node('execution-live-title').textContent,/持仓监控中/,'a newer successful account check clears the historical account error even while quotes are refreshing');
    assert.match(node('execution-live-time').textContent,/监控更新/);
    execution={...execution,close_only:true,tracked_position_count:0,managed_positions:[],latest_event:null};
    await node('ops-refresh').onclick();
    assert.equal(node('execution-trade-mode').value,'close_only','a reload must reflect the actual running mode');
    assert.equal(node('execution-trade-mode').disabled,true,'a running mode cannot be edited in place');
    assert.match(node('execution-live-title').textContent,/只平仓监控中.*无待管理仓位/);
    assert.doesNotMatch(node('execution-live-detail').textContent,/达到入场门槛|读取新鲜双平台报价/);
    assert.equal(node('live-arm-label').textContent,'结束只平仓监控');
    assert.equal(node('execution-stop').hidden,true);
    assert.match(node('execution-process-state').textContent,/仅管理已有仓位，不开新仓/);
    node('execution-trade-mode').value='open_close';node('execution-trade-mode').onchange();
    assert.equal(node('execution-trade-mode').value,'close_only');
    execution={...execution,stop_requested:true};
    await node('ops-refresh').onclick();
    assert.match(node('execution-live-title').textContent,/已停止开仓.*等待确认退出/);
    assert.equal(node('live-arm').disabled,true);
    execution={...execution,stop_requested:false};
    execution.recovery_blocked=true;
    await node('ops-refresh').onclick();
    assert.match(node('execution-live-title').textContent,/原仓位待接管/);
    assert.match(node('execution-mode').textContent,/旧仓位待接管/);
    execution={...execution,venues:selected.slice(),running:false,state:'finished',stop_requested:true,
        stop_requires_reconcile:true};
    await node('ops-refresh').onclick();
    assert.match(node('execution-live-title').textContent,/已停止.*仓位待核对/);
    assert.match(node('execution-mode').textContent,/已停止.*仓位待核对/);
    assert.doesNotMatch(node('live-arm-label').textContent,/停止中/);
    assert.equal(node('execution-reset-stop').disabled,false,'stopped pending position must allow read-only recovery');
    assert.equal(node('execution-check').disabled,false);
    assert.equal(node('execution-trade-mode').disabled,false);
    assert.equal(node('execution-trade-mode').value,'open_close','runtime readback must not silently select close-only for the next run');
    execution={...execution,venues:['lighter','lighter_rh'],stop_requires_reconcile:false};
    execution.stop_completed=true;
    execution.latest_event={event:'execution_stop_completed',timestamp:'2026-01-01T00:00:00+00:00'};
    await node('ops-refresh').onclick();
    assert.equal(node('execution-live-title').textContent,'全局实盘已停止');
    assert.equal(node('execution-mode').textContent,'全局实盘已停止');
    assert.doesNotMatch(node('live-arm-label').textContent,/停止中/);
    await node('ops-refresh').onclick(); await node('execution-reset-stop').onclick();
    const reset=requests.find(r=>r.path.endsWith('/reset-stop'));
    assert.deepEqual(reset.payload.venues,['lighter','lighter_rh'],'reset must verify original accounts');
    assert.deepEqual(requests.filter(r=>/\/(once|wait-once|monitor|stop)$/.test(r.path)).map(r=>r.path),
        ['/api/execution/monitor'],'only the explicit simulated launch above may start; selection edits never start or stop');
    assert.match(node('execution-entry-risk').textContent,/旧订单不推测归类/);
    execution.entry_risk_routes=[{route:'UNI|arcus->lighter',samples:5,movement_bps:3.2,window_ms:1250,
        counts:{edge:2,zero_fill:1},size_cap_usdc:6}];
    await node('ops-refresh').onclick();
    assert.match(node('execution-entry-risk').textContent,/净价差不足 2 次.*零成交 1 次/);
    assert.match(node('execution-entry-risk').title,/UNI.*P90 3.20 bps \/ 1250 ms.*6.00 USDC/);
    execution.running=true;
    execution.position_monitor={entry_budget_policy_version:2};
    await node('ops-refresh').onclick();
    assert.match(node('execution-entry-risk').textContent,/历史执行窗口仅作观察/);
    assert.doesNotMatch(node('execution-entry-risk').textContent,/尚未加载/);
    execution.position_monitor={};
    await node('ops-refresh').onclick();
    assert.match(node('execution-entry-risk').textContent,/当前执行进程尚未加载此规则/);
    selected=['lighter','arcus'];
    userScope={mode:'all',symbols:[],excluded_symbols:[]};
    selection={mode:'all',symbols:['UNI','NEAR','AAVE']};
    execution={running:true,state:'running',mode:'continuous_monitor',venues:selected.slice(),symbols:['UNI']};
    const requestsBeforeScope=requests.length;
    await node('ops-refresh').onclick();
    assert.match(node('execution-auto-state').textContent,/品种：1 个（UNI）/);
    assert.match(node('execution-auto-state').textContent,/另有 2 个未纳入本次运行/);
    assert.match(node('execution-auto-state').textContent,/下次重新检查并启动/);
    assert.equal(requests.slice(requestsBeforeScope).some(r=>/\/(once|wait-once|monitor|stop)$/.test(r.path)),false,
        'coverage warnings never restart or enlarge a running live worker');
    execution={running:false,state:'finished'};
    await node('ops-refresh').onclick();
    assert.match(node('execution-auto-state').textContent,/共同品种 3 个/);
    assert.match(node('execution-auto-state').textContent,/不受页面当前显示行数限制/);
    execution={running:true,state:'running',mode:'continuous_monitor',pid:99,started_at:'new-run',
        venues:['lighter','lighter_rh'],symbols:['UNI'],position_monitor:{heartbeat_stale:false,
            runtime_scope:{version:1,state:'applied',entry_venues:['lighter','lighter_rh'],symbols:['UNI']}}};
    selected=['lighter','arcus'];
    const scopeRequests=()=>requests.filter(r=>r.path.endsWith('/scope'));
    await node('ops-refresh').onclick();
    assert.equal(scopeRequests().length,0,'status refresh never changes a live scope');
    events.get('execution-platform-scope-ready')();
    await new Promise(resolve=>setTimeout(resolve,390));
    assert.equal(scopeRequests().length,1);
    assert.deepEqual(scopeRequests()[0].payload,{pid:99,started_at:'new-run',venues:selected,symbols:['UNI','NEAR','AAVE']});
    await node('ops-refresh').onclick();
    assert.match(node('execution-scope-result').textContent,/后台核验/,'keep queued request until worker acknowledgement');
    execution.position_monitor.runtime_scope={version:1,state:'applied',request_id:'scope-1',
        entry_venues:selected.slice(),symbols:selection.symbols.slice(),message:'新仓范围已更新'};
    await node('ops-refresh').onclick();
    assert.equal(node('execution-apply-scope').hidden,true);
    assert.match(node('execution-scope-result').textContent,/已更新/);

    scopeResponse='defer';selected=['lighter','lighter_rh','arcus'];
    const pendingScope=node('execution-apply-scope').onclick();await tick();
    selected=['lighter_rh','arcus'];
    const queuedScope=node('execution-apply-scope').onclick();await tick();
    assert.equal(scopeRequests().length,2,'only one scope POST in flight');
    scopeResponse=null;releaseScope({success:true,request_id:'scope-2',message:'后台准备'});
    await pendingScope;await queuedScope;
    assert.equal(scopeRequests().length,3);
    assert.deepEqual(scopeRequests().at(-1).payload.venues,selected,'latest selection wins');
    execution.position_monitor.runtime_scope=null;
    await node('ops-refresh').onclick();
    await node('execution-apply-scope').onclick();
    assert.equal(scopeRequests().length,3,'old worker cannot silently apply new scope');
    assert.equal(node('execution-apply-scope').disabled,true);
    assert.equal(requests.filter(r=>/\/(once|wait-once|monitor|stop)$/.test(r.path)).length,1,
        'scope changes neither start nor stop trading');
    console.log('Platform switch: stable checks, background live scope, queued acknowledgement, latest selection and legacy worker guards PASS; no orders');
})().catch(error=>{console.error(error);process.exitCode=1;});
