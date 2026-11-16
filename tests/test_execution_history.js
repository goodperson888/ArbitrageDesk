'use strict';
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
class Element {
    constructor(tag) { this.tag = tag; this.children = []; this.listeners = {}; this.textContent = ''; this.dataset = {}; this.scrollLeft = 0; this.scrollTop = 0; }
    appendChild(child) { this.children.push(child); return child; }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; this.scrollLeft=0; this.scrollTop=0; }
    setAttribute(key, value) { this[key] = value; }
    addEventListener(key, callback) { this.listeners[key] = callback; }
    get text() { return this.textContent + this.children.map(child => child.text || '').join(' '); }
    find(tag) { return this.children.flatMap(child => [ ...(child.tag === tag ? [child] : []), ...child.find(tag)]); }
    querySelectorAll(selector) { assert.equal(selector, '[data-history-scroll-key]'); return this.find('div').filter(child => child.dataset.historyScrollKey); }
}
const root = new Element('root');
const base = {symbol:'BABA',status:'closed',open_time:'2026-11-01T10:00:00',entry_size_variational:1,
    entry_size_lighter:1,entry_price_variational:100,entry_price_lighter:99,variational_side:'short',
    lighter_side:'buy',variational_venue:'lighter',hedge_status:'paired',realized_pnl_usdc:null};
const report = {scope:'all',summary:{closed_count:2,pending_accounting_count:1},
    available_pairs:[['lighter','arcus'],['lighter','lighter_rh']],
    open_positions:[{...base,id:'open',status:'open',lighter_venue:'arcus',current_spread_bps:12.34}],
    closed_positions:[{...base,id:'arcus',lighter_venue:'arcus',accounting_pending:true,
                       accounting_subtotal_usdc:.048632,accounting_missing:['lighter 平仓手续费（订单 missing-fee）'],accounting_note:'缺平仓手续费',close_spread_bps:8.097,current_spread_bps:999,recovered_close_fills:{lighter:{average_price:98.72}}},
                      {...base,id:'rh',lighter_venue:'lighter_rh',hedge_status:'single_leg_recovered'}],
    other_fills:[{venue:'arcus',symbol:'BABA',source:'arcus_round_trip',reduce_only:true,
        side:'sell',filled_size:.05,average_price:101,fees_usdc:.001,status:'filled',order_id:'arcus-close'}]};
let requested, deferredFetch=null;
const drawerUpdates=[];
const closeActionPositions=[];
const closeActionCells=[];
const context = vm.createContext({window:{},appendManualCloseAction:(position,cell)=>{closeActionPositions.push(position.id);closeActionCells.push(cell);},updateOpportunityHistoryPositions:rows=>drawerUpdates.push(rows),document:{getElementById:()=>root,addEventListener(){},
    createElement:tag=>new Element(tag)},fetch:async path=>{requested=path;if(deferredFetch)return deferredFetch;return {ok:true,json:async()=>report};},console,Date});
vm.runInContext(fs.readFileSync('src/web/static/js/app.js','utf8'),context);
(async()=>{
    await vm.runInContext('loadExecutionHistory()',context);
    assert.deepEqual(closeActionPositions,['open'],'only unclosed groups get the double-close action');
    const initialOrderRows=root.find('tr').filter(row=>row.className==='execution-history-row');
    const actionHeaders=root.find('th').filter(cell=>cell.className==='history-actions');
    assert.equal(actionHeaders.length,1);
    assert.equal(actionHeaders[0].textContent,'操作');
    assert.ok(initialOrderRows.every(row=>row.children.length===13),'every order row retains a dedicated action column');
    assert.equal(closeActionCells[0],initialOrderRows[0].children[12],'double close belongs in the rightmost action cell');
    assert.equal(closeActionCells[0].className,'history-actions');
    assert.equal(closeActionCells[0].textContent,'','open action cell has no empty-value placeholder beside its button');
    assert.ok(!closeActionCells.includes(initialOrderRows[0].children[0]),'position status stays separate from actions');
    assert.ok(initialOrderRows.slice(1).every(row=>row.children[12].textContent==='—'),'closed orders have no double-close action');

    assert.equal(requested,'/api/execution-history?event_page_size=200');
    assert.match(root.text,/全部平台组合/);
    assert.match(root.text,/卖出 Lighter ↔ 买入 Arcus/);
    assert.match(root.text,/卖出 Lighter ↔ 买入 Lighter RH/);
    assert.match(root.text,/数量（第一腿 \/ 第二腿）/,'mixed pair columns must not reuse the first row venue names');
    assert.match(root.text,/已平仓·待核账/);
    assert.match(root.text,/暂计 \+0\.0486 USDC/);
    assert.match(root.text,/lighter 平仓手续费/);
    assert.match(root.text,/当前 \/ 平仓价差/);
    assert.match(root.text,/平仓：\+8\.10 bps/,'actual close spread is shown even while fee settlement is pending');
    assert.match(root.text,/当前：\+12\.34 bps/,'open rows explicitly identify the current spread');
    assert.doesNotMatch(root.text,/999\.00 bps/,'closed rows use confirmed closing spread rather than current quotes');
    report.open_positions[0].entry_target={target_mode:'center',target_bps:-30,frozen_at:'2026-11-04T12:00:00+08:00'};
    report.open_positions[0].target_entry_net_bps=7.8;
    report.open_positions[0].actual_entry_spread_bps=-10;
    await vm.runInContext('loadExecutionHistory()',context);
    const entryCell=root.find('td').find(cell=>cell.textContent.startsWith('-10.00 bps'));
    assert.match(entryCell.title,/冻结回归目标：历史中心 -30.00 bps/);
    assert.match(entryCell.title,/净回归空间：\+7.80 bps/);
    assert.match(entryCell.title,/不等于最终已实现盈亏/);
    assert.match(root.text,/不适用/,'single-leg recoveries have no paired closing spread');
    report.closed_positions[1].rollback_explanation={text:'第1腿 Lighter 卖出：下单数量 2；成交 2\n第2腿 Lighter RH 买入：未提交\n回补原因：<img src=x onerror=bad> 保护边界\n复核时剩余 0.18 bps；实际提交限价 0.093293\n原始状态 CANCELED；指令 IOC\n首腿确认→第二腿发送：188 ms\n第1腿回补平仓成交：2'};
    await vm.runInContext('loadExecutionHistory()',context);
    const recoveryCell=root.find('td').find(cell=>cell.textContent==='已回补·未成对');
    assert.match(recoveryCell.title,/下单数量 2/);
    assert.match(recoveryCell.title,/第2腿 Lighter RH/);
    assert.match(recoveryCell['aria-label'],/未提交/);
    assert.match(recoveryCell.title,/复核时剩余 0.18 bps/);
    assert.match(recoveryCell.title,/原始状态 CANCELED；指令 IOC/);
    assert.match(recoveryCell.title,/首腿确认→第二腿发送：188 ms/);
    assert.match(recoveryCell.title,/第1腿回补平仓成交：2/);
    assert.equal(recoveryCell.innerHTML,undefined,'tooltip strings are assigned as text, never HTML');
    report.closed_positions[0].close_diagnostics={text:'原触发：价差收敛且达到盈利；最终补平亏损',outcome:{summary:'补平后亏损',text:'首次 0 成交；补平 0.59 @ 20.276 <img src=x onerror=bad>'}};
    await vm.runInContext('loadExecutionHistory()',context);
    const residualNote=root.find('small').find(cell=>cell.textContent==='补平后亏损');
    assert.ok(residualNote,'the closed status distinguishes the residual close outcome');
    assert.match(residualNote.title,/首次 0 成交；补平 0.59/);
    assert.equal(residualNote.innerHTML,undefined,'outcome evidence is rendered as plain text');
    assert.match(root.text,/单平台测试 · 只减仓平仓/);
    assert.match(root.text,/arcus-close/);
    const scrollWrap=key=>root.querySelectorAll('[data-history-scroll-key]').find(wrap=>wrap.dataset.historyScrollKey===key);
    scrollWrap('orders').scrollLeft=640;
    scrollWrap('orders').scrollTop=120;
    scrollWrap('other-fills').scrollLeft=210;
    root.scrollTop=250;
    await vm.runInContext('loadExecutionHistory()',context);
    assert.equal(scrollWrap('orders').scrollLeft,640,'automatic refresh preserves order table horizontal scroll');
    assert.equal(scrollWrap('orders').scrollTop,120,'automatic refresh preserves order table vertical scroll');
    assert.equal(scrollWrap('other-fills').scrollLeft,210,'each history table retains its own scroll position');
    assert.equal(root.scrollTop,250,'rebuilding the history panel preserves its outer scroll position');
    let releaseScroll;
    deferredFetch=new Promise(resolve=>{releaseScroll=resolve;});
    const pendingScroll=vm.runInContext('loadExecutionHistory()',context);
    scrollWrap('orders').scrollLeft=820;
    deferredFetch=null;
    releaseScroll({ok:true,json:async()=>report});
    await pendingScroll;
    assert.equal(scrollWrap('orders').scrollLeft,820,'scrolling while a refresh is pending keeps the latest position');
    const savedFills=report.other_fills;
    report.other_fills=[];
    await vm.runInContext('loadExecutionHistory()',context);
    assert.equal(scrollWrap('orders').scrollLeft,820,'removing an optional table does not reset order scroll');
    report.other_fills=savedFills;
    report.closed_positions[0].execution_id='timed';
    report.closed_positions[0].execution_elapsed_ms=18189;
    // Server events arrive newest first. Durations include overlapping scopes.
    report.events=[
        {event:'stage_completed',timestamp:'2026-11-03T03:35:56.152325Z',detail:{execution_id:'timed',stage:'Arcus 成交回执查询',elapsed_ms:15602}},
        {event:'stage_started',timestamp:'2026-11-03T03:35:40.053338Z',detail:{execution_id:'timed',stage:'Arcus 签名发送'}},
        {event:'hedge_decision',timestamp:'2026-11-03T03:35:40.052Z',detail:{execution_id:'timed',decision:'hedge'}},
        {event:'stage_completed',timestamp:'2026-11-03T03:35:40.050964Z',detail:{execution_id:'timed',stage:'Arcus 深度报价',elapsed_ms:99}},
        {event:'entry_receipt',timestamp:'2026-11-03T03:35:39.948877Z',detail:{execution_id:'timed',venue:'lighter',filled_size:3065}},
        {event:'stage_completed',timestamp:'2026-11-03T03:35:39.947711Z',detail:{execution_id:'timed',stage:'Lighter 成交回执查询',elapsed_ms:940}},
    ];
    await vm.runInContext('loadExecutionHistory()',context);
    const timedRow=root.find('tr').find(row=>row.title?.includes('首腿确认→次腿发送'));
    assert.ok(timedRow,'the receipt-to-submit delay is shown on the order');
    assert.match(timedRow.title,/首腿确认→次腿发送 105 ms/); // Date.parse has millisecond precision.
    assert.match(timedRow.title,/含并行或嵌套，不可相加/);
    assert.doesNotMatch(timedRow.title,/阶段合计/);
    assert.match(timedRow.text,/次腿发送 \+105 ms/);
    assert.ok(timedRow.title.indexOf('Lighter 成交回执查询')<timedRow.title.indexOf('Arcus 深度报价'));
    assert.ok(timedRow.title.indexOf('Arcus 深度报价')<timedRow.title.indexOf('Arcus 成交回执查询'));
    report.events=report.events.filter(row=>row.event!=='hedge_decision');
    await vm.runInContext('loadExecutionHistory()',context);
    assert.doesNotMatch(root.text,/次腿发送/,'a recovery send without a hedge decision must not be labeled second-leg submission');
    const selector=root.find('select')[0];
    selector.value='lighter,arcus';
    selector.listeners.change();
    await new Promise(resolve=>setImmediate(resolve));
    assert.match(requested,/venues=lighter%2Carcus/);
    report.events=[{event:'hedge_decision',timestamp:'2026-11-02T13:00:00Z',detail:{symbol:'UNI',
        observed_price:100.01,protected_bound:100.02,hedge_edge_usdc:.01,unwind_pnl_estimate_usdc:-.005}},
        {event:'route_rollback_paused',detail:{symbol:'UNI',rollback_count:2,paused_until:1793630000}}];
    await vm.runInContext("executionHistoryView='events';loadExecutionHistory()",context);
    assert.match(root.text,/首腿成交后对冲评估/);
    assert.match(root.text,/对冲价差净值（非结算）/);
    assert.match(root.text,/预计立即回补损益/);
    assert.match(root.text,/连续回补 · 该路线已暂停/);
    assert.match(root.text,/暂停至/);
    await vm.runInContext("executionHistoryView='all';loadExecutionHistory()",context);
    assert.ok(drawerUpdates.length>0,'accepted history refresh updates the open drawer');
    report.open_positions[0].current_spread_bps=null;
    report.open_positions[0].unrealized_pnl_usdc=null;
    report.open_positions[0].mark_error='arcus：行情限流，等待新报价';
    await vm.runInContext('loadExecutionHistory()',context);
    assert.match(root.text,/当前：arcus：行情限流/);
    assert.match(root.text,/中间价浮盈（非平仓收益） · 未扣未来平仓费/);
    Object.assign(report.closed_positions[0],{history_recovered:true,closed_time:'2026-11-03T06:50:17Z',close_elapsed_ms:2436609});
    report.history_sync={state:'partial',message:'部分平台历史暂未读全',retry_after_seconds:180,
        errors:[{venue:'lighter',error:'RateLimitError'}]};
    await vm.runInContext("executionHistoryView='closed';loadExecutionHistory()",context);
    assert.match(root.text,/已平仓·待核账/,'closed rows with pending accounting stay in the closed filter');
    assert.match(root.text,/平台成交已补齐/);
    assert.match(root.text,/含手动处理等待/);
    assert.match(root.text,/Lighter 限流/);
    assert.match(root.text,/平仓 /,'entry and final closing times are separately labeled');
    report.open_positions[0].status='reconcile_required';
    report.open_positions[0].accounting_pending=true;
    await vm.runInContext("executionHistoryView='open';loadExecutionHistory()",context);
    assert.match(root.text,/当前：arcus：行情限流/,'unclosed rows needing attention stay in the open filter');
    await vm.runInContext("executionHistoryView='all';loadExecutionHistory()",context);
    const originalOpen=report.open_positions, originalClosed=report.closed_positions;
    report.open_positions=[
        {...base,id:'old-open',symbol:'PIN-OPEN',status:'open',open_time:'2026-10-01T10:00:00Z'},
        {...base,id:'partial',symbol:'PIN-PARTIAL',status:'reconcile_required',partially_closed:true,
            open_time:'2026-10-02T10:00:00Z'},
        {...base,id:'closing',symbol:'PIN-CLOSING',status:'closing',open_time:'2026-10-03T10:00:00Z'},
    ];
    report.closed_positions=Array.from({length:30},(_,i)=>({...base,id:'closed-'+i,symbol:'DONE-'+i,
        accounting_pending:true,open_time:'2026-11-10T10:00:00Z',
        closed_time:new Date(Date.UTC(2026,10,11,0,i)).toISOString()}));
    await vm.runInContext('loadExecutionHistory()',context);
    const orderRows=()=>root.find('tr').filter(row=>row.className==='execution-history-row');
    assert.equal(orderRows().length,20);
    assert.match(orderRows()[0].text,/PIN-CLOSING/);
    assert.match(orderRows()[1].text,/PIN-PARTIAL/);
    assert.match(orderRows()[2].text,/PIN-OPEN/,'old active positions stay ahead of all new closed rows');
    assert.match(orderRows()[3].text,/DONE-29/,'completed rows retain descending closing-time order');
    await vm.runInContext('loadExecutionHistory()',context);
    assert.match(orderRows()[0].text,/PIN-CLOSING/,'automatic refresh keeps live exposure at the top');
    await vm.runInContext('executionHistoryPage=2;loadExecutionHistory()',context);
    assert.equal(orderRows().length,13,'pagination counts every order once after global sorting');
    assert.ok(orderRows().every(row=>!row.text.includes('PIN-')));
    report.open_positions=originalOpen;report.closed_positions=originalClosed;
    await vm.runInContext('executionHistoryPage=1;loadExecutionHistory()',context);
    let release;
    deferredFetch=new Promise(resolve=>{release=resolve;});
    const pending=vm.runInContext('loadExecutionHistory()',context);
    await vm.runInContext('loadExecutionHistory()',context);
    assert.equal(vm.runInContext('executionHistoryLoading',context),true);
    const before=drawerUpdates.length;
    vm.runInContext("executionHistoryPair='lighter,lighter_rh'",context);
    deferredFetch=null;
    release({ok:true,json:async()=>report});
    await pending;
    await new Promise(resolve=>setImmediate(resolve));
    assert.match(requested,/venues=lighter%2Clighter_rh/);
    assert.equal(drawerUpdates.length,before+1,'old selection response is discarded; only queued fresh response is rendered');
    console.log('Execution history scope, mixed venue labels, accounting and standalone fills: PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
