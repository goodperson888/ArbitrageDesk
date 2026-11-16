'use strict';
const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
class Element {
    constructor(){this.style={};this.children=[];this.textContent='';this.listeners={};this.classList={add(){},remove(){}};}
    appendChild(child){this.children.push(child);child.parentNode=this;return child;}
    append(...children){this.children.push(...children);}
    replaceChildren(){for(const child of this.children)child.parentNode=null;this.children=[];}
    setAttribute(){}
    addEventListener(name,fn){this.listeners[name]=fn;}
    querySelector(){return this.bold||(this.bold=new Element());}
    get text(){return this.textContent+(this.bold?.text||'')+this.children.map(c=>c.text).join(' ');}
}
const nodes=new Map(['opportunity-drawer','opportunity-drawer-title','opportunity-drawer-body'].map(id=>[id,new Element()]));
let now=Date.now(); const timers=[], quoteRequests=[], otherRequests=[], storage=new Map();
const context=vm.createContext({window:{QuoteFreshness:{describe:()=>({buyLabel:'新鲜',sellLabel:'新鲜'})}},
    document:{getElementById:id=>nodes.get(id),createElement:()=>new Element(),addEventListener(){}},
    spreadText:v=>Number.isFinite(v)?v.toFixed(2)+' bps':'--',durationText:v=>String(v)+' ms',localStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)},
    fetch:url=>new Promise(resolve=>{(url.includes('/quotes?')?quoteRequests:otherRequests).push({url,resolve});}),
    setInterval:fn=>timers.push(fn),clearInterval(){},console,Date:class extends Date {static now(){return now;}}});
vm.runInContext(fs.readFileSync('src/web/static/js/quote_freshness.js','utf8'),context);
const drawerSource=fs.readFileSync('src/web/static/js/cross_venue.js','utf8').replace(/\}\)\(\);\s*$/,
    'window.testChannelSpec=drawerChannelSpec;window.testChannelState=drawerChannelState;window.testChannelNote=channelChartNote;window.testRenderChart=renderReversionChart;window.testCloseDrawer=closeDrawer;window.testOpenDrawer=openDrawer;window.testQuoteUpdate=rows=>drawerQuoteUpdate?.(rows.map(refreshReversionEstimate));})();');
vm.runInContext(drawerSource,context);
let chartOptions, chartInits=0;
const echarts={init:()=>{chartInits++;return {setOption:option=>{chartOptions=option;},getOption:()=>({dataZoom:[{type:'inside',start:25,end:75}]}),dispose(){}};}};
context.window.echarts=echarts;
const chartRoot=new Element();
const history=[{timestamp:100,gross_bps:10},{timestamp:101,gross_bps:99,valid_for_center:false}];
context.window.testRenderChart(chartRoot,history,{center_bps:null,p10_bps:null,p90_bps:null,center_ready:false});
assert.deepEqual(Array.from(chartOptions.series[0].markLine.data,item=>item.name),['0'],
    'an unknown center cannot draw a zero center or regression band');
assert.equal(chartOptions.series[0].data.length,1,'stale observations do not enter the normal price line');
context.window.testRenderChart(chartRoot,history,{center_bps:10,p10_bps:9,p90_bps:11,center_ready:false});
assert.match(chartOptions.series[0].markLine.data[0].name,/临时中心/);
context.window.testRenderChart(chartRoot,history,{center_bps:10},null,[],-25.84);
assert.equal(chartOptions.series[0].markLine.data.find(item=>item.name==='本单冻结目标').yAxis,-25.84);
assert.equal(chartOptions.series[0].markLine.data.find(item=>item.name==='历史中心').yAxis,10);
const policy={method:'quantile',window_seconds:3600};
context.window.StrategyChannelUI={basis:()=>({mode:'channel',policy})};
const channel={axis_venues:['arcus','lighter'],lower_bps:-150,upper_bps:200,center_bps:10,exit_axis_bps:6,side:'lower',entry_axis_bps:-180};
const channelRow={canonical_symbol:'SUI-PERP',buy_venue:'lighter',sell_venue:'arcus',buy_price:100,sell_price:100,gross_bps:0,
 buy_age_seconds:0,sell_age_seconds:0,return_inputs:{buy:{ask:100,bid:99},sell:{ask:101,bid:100}},
 return_preview:{target_mode:'channel',channel_policy:policy,channel,meets_floor:false,reason:'整轮收益不足'}};
const channelSpec=context.window.testChannelSpec(channelRow);
const lowerContext=context.window.testChannelState(channelRow,channelSpec);
assert.equal(lowerContext.current,(99-101)/101*10000,'reverse route uses canonical A ask / B bid, preserving both spreads');
assert.notEqual(lowerContext.current,-channelRow.gross_bps,'reverse chart is not a sign inversion');
assert.match(context.window.testChannelNote(lowerContext),/已触边；只读估算：整轮收益不足/);
const beforeInit=chartInits;
context.window.testRenderChart(chartRoot,history,{center_bps:999,p10_bps:888},null,[],null,lowerContext);
assert.equal(chartInits,beforeInit,'live chart updates reuse the existing ECharts instance');
assert.equal(chartOptions.dataZoom[0].start,25,'live updates preserve chart zoom');
assert.deepEqual(Array.from(chartOptions.series[0].markLine.data,item=>item.name),['上边界','下边界','通道中心','0']);
assert.equal(chartOptions.series[0].markLine.data[0].yAxis,200,'use evaluated channel bounds, not seven-day P10/P90');
assert.ok(Math.abs(chartOptions.series[0].data[0][1]-10/1.001)<1e-10,'stored positive ratio spread is converted to execution denominator');
const frozenSpec=context.window.testChannelSpec(channelRow,{entry_target:{channel,target_bps:512}});
context.window.StrategyChannelUI={basis:()=>({mode:'fixed',policy:{}})};
const frozenContext=context.window.testChannelState(channelRow,frozenSpec);
context.window.testRenderChart(chartRoot,history,{center_bps:999},null,[],512,frozenContext);
assert.equal(chartOptions.series[0].markLine.data.find(item=>item.name==='本单冻结目标').yAxis,6,'frozen exit uses canonical axis, not directed target');
assert.equal(chartOptions.series[0].markLine.data.find(item=>item.name==='冻结上界').yAxis,200,'global changes do not move an existing position boundary');
context.window.StrategyChannelUI={basis:()=>({mode:'channel',policy})};
const missingContext=context.window.testChannelState({...channelRow,return_preview:{target_mode:'channel',channel_policy:policy,reason:'通道有效样本不足 3/30'}},channelSpec);
context.window.testRenderChart(chartRoot,history,{center_bps:999,p10_bps:888},null,[],null,missingContext);
assert.deepEqual(Array.from(chartOptions.series[0].markLine.data,item=>item.name),['0'],'missing samples never fall back to unrelated statistics');
assert.match(chartRoot.text,/边界未就绪.*3\/30/);
const staleContext=context.window.testChannelState({...channelRow,buy_age_seconds:6},channelSpec);
assert.equal(staleContext.current,null);
assert.match(context.window.testChannelNote(staleContext),/暂不判断触线/);
assert.doesNotMatch(context.window.testChannelNote(staleContext),/已触边/);
context.window.StrategyChannelUI={basis:()=>({mode:'channel',policy:{...policy,window_seconds:900}})};
assert.equal(context.window.testChannelState(channelRow,channelSpec).channel,null,'changed settings invalidate the old opportunity boundary');
// Every overlay can be hidden independently without changing execution data or fetching.
const controlHistory=[...history,{timestamp:102,gross_bps:80,anomalous:true}];
const controlCurrent={timestamp:103,gross_bps:30}, controlStats={center_bps:10,p10_bps:9,p90_bps:11,events:[{timestamp:102,start_timestamp:100}]};
const reference=[{timestamp:100,reference_bps:12}];
context.window.testRenderChart(chartRoot,controlHistory,controlStats,controlCurrent,reference,-25.84);
const controls=chartRoot._chartControls, requestCount=quoteRequests.length+otherRequests.length;
controls.open=true;
const toggleLine=(key,checked)=>{const input=chartRoot._chartInputs.get(key);assert.ok(input,key+' control exists');input.checked=checked;input.listeners.change();};
toggleLine('center',false);toggleLine('target',false);toggleLine('bandUpper',false);toggleLine('reference',false);toggleLine('anomalies',false);
assert.equal(chartOptions.series[0].markLine.data.some(mark=>['历史中心','本单冻结目标','+回归带'].includes(mark.name)),false);
assert.ok(chartOptions.series[0].markLine.data.some(mark=>mark.name==='-回归带'),'upper/lower regression bands toggle independently');
assert.equal(chartOptions.series.length,1,'hidden optional series do not affect chart scaling');
assert.equal(controlStats.center_bps,10);assert.equal(controlCurrent.gross_bps,30,'visibility cannot mutate prices or boundaries');
assert.equal(chartRoot._chartControls,controls);assert.equal(controls.open,true,'live redraw keeps controls expanded');
assert.equal(chartOptions.dataZoom[0].start,25,'toggle keeps zoom');
assert.equal(chartRoot._chartInputs.get('center').checked,false);
assert.equal(JSON.parse(storage.get('desk.detailChart.lines.v1')).target,false,'choices are persisted');
context.window.testRenderChart(chartRoot,controlHistory,controlStats,controlCurrent,reference,-25.84);
assert.equal(chartOptions.series[0].markLine.data.some(mark=>mark.name==='历史中心'),false,'stream repaint does not re-enable hidden lines');
context.window.testRenderChart(chartRoot,controlHistory,controlStats,{timestamp:104,gross_bps:41},reference,-25.84);
toggleLine('spread',false);assert.equal(chartOptions.series[0].lineStyle.opacity,0);
assert.ok(chartOptions.series[0].data.length>0,'invisible anchors retain time/value axes for standalone strategy marks');
assert.equal(chartOptions.series[0].data.at(-1)[1],41,'existing controls redraw the latest delivered quote');
assert.equal(chartOptions.tooltip.formatter([{seriesIndex:0,value:[103000,30]}]),'','hidden anchors do not produce price tooltips');
assert.ok(chartOptions.series[0].markPoint,'current marker is independent of spread line');
toggleLine('current',false);assert.equal(chartOptions.series[0].markPoint,undefined);
const reopenedRoot=new Element();
context.window.testRenderChart(reopenedRoot,history,controlStats,null,reference,512,frozenContext);
assert.equal(reopenedRoot._chartInputs.get('target').checked,false,'reopening another order retains choices');
assert.equal(chartOptions.series[0].markLine.data.some(mark=>mark.name==='本单冻结目标'),false);
assert.equal(frozenContext.channel.exit_axis_bps,6,'hiding a frozen target does not change it');
const options=reopenedRoot._chartOptions, actions=options.children.find(child=>child.className==='chart-line-actions');
actions.children[0].listeners.click();
assert.deepEqual(Array.from(chartOptions.series[0].markLine.data,mark=>mark.name),['冻结上界','冻结下界','冻结中心','本单冻结目标'],'strategy preset removes optional overlays');
actions.children[1].listeners.click();
assert.equal(chartOptions.series[0].markLine.data.length,5);
assert.equal(quoteRequests.length+otherRequests.length,requestCount,'display controls never issue requests');
assert.equal(reopenedRoot._chartInputs.has('p10'),false,'controls only offer overlays relevant to the chart');
reopenedRoot._chartInputs.get('target').checked=false;reopenedRoot._chartInputs.get('target').listeners.change();
const reloadContext=vm.createContext({window:{echarts,QuoteFreshness:context.window.QuoteFreshness},document:context.document,localStorage:context.localStorage,console,Date});
vm.runInContext(drawerSource,reloadContext);
const restoredRoot=new Element();reloadContext.window.testRenderChart(restoredRoot,history,{},null,[],512,frozenContext);
assert.equal(restoredRoot._chartInputs.get('target').checked,false,'a full script reload restores saved overlay choices');
const blockedStorage=vm.createContext({window:{echarts,QuoteFreshness:context.window.QuoteFreshness},document:context.document,
    localStorage:{getItem(){throw Error('blocked');},setItem(){throw Error('blocked');}},console,Date});
vm.runInContext(drawerSource,blockedStorage);
const privateRoot=new Element();blockedStorage.window.testRenderChart(privateRoot,history,{},null,[],512,frozenContext);
privateRoot._chartInputs.get('target').checked=false;privateRoot._chartInputs.get('target').listeners.change();
assert.equal(privateRoot._chartInputs.get('target').checked,false,'blocked browser storage retains usable in-memory controls');
actions.children[1].listeners.click(); // Restore default drawing choices for the remaining drawer tests.
delete context.window.StrategyChannelUI;
delete context.window.echarts;
const position={id:'original',symbol:'PUMP',status:'open',variational_venue:'lighter',lighter_venue:'arcus',
    variational_side:'short',lighter_side:'buy',entry_price_variational:103,entry_price_lighter:100,
    variational_size:2,lighter_size:2,current_spread_bps:null,unrealized_pnl_usdc:null,mark_error:'arcus：行情限流'};
context.window.openOpportunityHistoryDrawer(position);
const body=nodes.get('opportunity-drawer-body'), grid=body.children[0];
assert.match(body.text,/arcus：行情限流/);
context.window.updateOpportunityHistoryPositions([{...position,current_spread_bps:20.47,unrealized_pnl_usdc:-.0112,mark_error:null}]);
assert.equal(body.children[0],grid,'updating history metrics preserves the chart and drawer DOM');
assert.match(body.text,/20\.47 bps/);
assert.match(body.text,/-0\.0112 U/);
context.window.updateOpportunityHistoryPositions([{...position,lighter_venue:'lighter_rh',current_spread_bps:999}]);
assert.doesNotMatch(body.text,/999/,'a different venue pair cannot replace the open position');
context.window.updateOpportunityHistoryPositions([{...position,mark_error:'arcus：行情过期'}]);
assert.match(body.text,/arcus：行情过期/,'failed new marks clear prior live metrics');
assert.doesNotMatch(body.text,/-0\.0112 U/);
context.window.testCloseDrawer();
context.window.updateOpportunityHistoryPositions([{...position,current_spread_bps:888}]);
assert.doesNotMatch(body.text,/888/,'closed drawers do not receive updates');
const recovered={...position,status:'closed',history_recovered:true,accounting_pending:true,
    entry_size_variational:2,entry_size_lighter:2,variational_size:0,lighter_size:0,
    close_price_variational:101,close_price_lighter:101,close_spread_bps:0,
    accounting_subtotal_usdc:.0077,realized_pnl_usdc:null};
context.window.openOpportunityHistoryDrawer(recovered);
assert.ok(body.children[0].children.some(item=>item.innerHTML.includes('开仓数量（两腿）')));
const remaining=body.children[0].children.find(item=>item.innerHTML.includes('本单剩余数量（两腿）'));
assert.equal(remaining.bold.textContent,'0 / 0');
assert.match(body.text,/平台历史成交与已确认回执自动补齐/);
assert.match(body.text,/暂计 0\.007700 U/);
context.window.updateOpportunityHistoryPositions([{...recovered,accounting_pending:false,realized_pnl_usdc:.0075}]);
assert.match(body.text,/0\.007500 U/,'settled accounting refreshes an already closed drawer');
assert.doesNotMatch(body.text,/暂计 0\.007700 U/);
context.window.updateOpportunityHistoryPositions([{...recovered,accounting_pending:false,realized_pnl_usdc:.0075,
    close_diagnostics:{text:'触发盘口 Bid / Ask 100 / 101\n发送前复核时间：测试\n实际平仓成交均价：101 / 101'}}]);
assert.match(body.text,/触发盘口 Bid \/ Ask 100 \/ 101/);
assert.match(body.text,/发送前复核时间/,'late background diagnostics update an already open closed-order drawer');
context.window.openOpportunityHistoryDrawer({...position,close_diagnostics:{text:'尚无已记录的平仓评估'}});
const openGrid=body.children[0];
context.window.updateOpportunityHistoryPositions([{...position,close_diagnostics:{text:'发送前复核：暂缓提交 <img src=x>'}}]);
assert.equal(body.children[0],openGrid,'diagnostic updates for an open order preserve the chart DOM');
assert.match(body.text,/发送前复核：暂缓提交 <img src=x>/);
const review=body.children.find(item=>item.className==='desk-disclosure drawer-close-review');
assert.ok(review,'close evidence remains available in an expandable section');
assert.notEqual(review.open,true,'routine evidence starts collapsed');
const diagnostic=review.children.find(item=>item.className==='chart-note drawer-wear drawer-close-diagnostics');
assert.equal(diagnostic.innerHTML,undefined,'diagnostic evidence renders as text');
vm.runInContext(fs.readFileSync('src/web/static/js/position_monitor.js','utf8'),context);
const monitor={convergence_ok:true,exit_profit_status:'waiting',exit_net_pnl_usdc:-.05178,
    exit_reference_net_pnl_usdc:-.0445,exit_movement_cost_usdc:.00728,exit_closing_fee_usdc:0,
    exit_estimate_at:new Date().toISOString()};
context.window.openOpportunityHistoryDrawer({...position,entry_target:{target_bps:-25.84},position_monitor:monitor});
assert.ok(body.children[0].children.some(item=>item.innerHTML.includes('本单冻结目标')));
assert.match(body.text,/粉色虚线/);
assert.match(body.text,/已回归 · 盘口平仓仍未盈利/);
assert.match(body.text,/-0.05178 U（价格保护后）/);
const monitorGrid=body.children[0];
context.window.updateOpportunityHistoryPositions([{...position,position_monitor:{...monitor,
    exit_reference_net_pnl_usdc:.001,exit_net_pnl_usdc:-.00628}}]);
assert.equal(body.children[0],monitorGrid,'profit state updates keep the chart and frozen target intact');
assert.match(body.text,/已回归 · 价格保护后净收益未达门槛/);
context.window.openOpportunityHistoryDrawer({...recovered,hedge_status:'single_leg_recovered',rollback_explanation:{text:'第一腿下单 2，第二腿未提交；保护价差不足'}});
assert.match(body.text,/第一腿下单 2，第二腿未提交/);
(async()=>{
 const preview={version:1,requested_target_mode:'center',window_seconds:604800,target_mode:'center',target_bps:12,
  net_bps:-17.4,profit_usdc:-.02088,minimum_bps:8,meets_floor:false,quantity:.009,reference_notional:12,
  movement_bps:2,costs:{open_fees_bps:4.5},reason:'整轮净收益 -17.40 bps，距入场门槛还差 25.40 bps'};
 const quote={canonical_symbol:'ZEC-PERP',buy_venue:'arcus',sell_venue:'lighter',buy_price:1284,sell_price:1287,
  gross_bps:23.3,net_bps:18.8,buy_age_seconds:0,sell_age_seconds:0,return_preview:preview,
  estimated_reversion_profit_usdc:preview.profit_usdc};
 const metric=label=>body.children[0].children.find(item=>item.innerHTML==='<span>'+label+'</span><b></b>').bold.textContent;
 const status=()=>body.children.find(item=>item.className?.startsWith('drawer-status'));
 context.window.testOpenDrawer(quote);
 const originalGrid=body.children[0];
 assert.equal(metric('整轮净价差'),'-0.1740%');
 assert.equal(metric('开仓净差（未计平仓）'),'0.1880%');
 assert.match(status().textContent,/距入场门槛还差 25.40/);
 assert.match(status().className,/warn/,'a positive opening net cannot mark an unprofitable full cycle green');
 const lateRequests=quoteRequests.slice(-2);
 const newer={...quote,buy_price:1285,gross_bps:22,return_preview:{...preview,net_bps:-18,profit_usdc:-.0216,reason:'最新整轮收益未达门槛'}};
 context.window.testQuoteUpdate([newer]);
 assert.equal(metric('买入 Ask'),'1,285');
 assert.equal(metric('整轮净价差'),'-0.1800%');
 assert.equal(body.children[0],originalGrid,'live updates preserve drawer and chart DOM');
 for(const request of lateRequests) request.resolve({ok:true,json:async()=>[{ask:100,bid:101,age_seconds:0}]});
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(metric('买入 Ask'),'1,285','a slow independent quote cannot overwrite a newer stream update');
 context.window.testQuoteUpdate([{...newer,buy_venue:'lighter',sell_venue:'arcus',buy_price:999}]);
 assert.equal(metric('买入 Ask'),'1,285','opposite routes cannot replace this route');
 const before=quoteRequests.length; await timers.at(-1)();
 assert.equal(quoteRequests.length,before,'a fresh scanner stream suppresses duplicate quote requests');
 now+=6000;
 context.window.testQuoteUpdate([{...newer,buy_age_seconds:6}]);
 assert.equal(metric('整轮净价差'),'未知');
 assert.match(status().textContent,/报价超过实盘估算时效/);
 context.window.testCloseDrawer();context.window.testQuoteUpdate([quote]);
 assert.equal(metric('整轮净价差'),'未知','closed drawers stop receiving quote updates');
 // Independent quote updates can refresh BBO, but have no new full model.
 context.window.testOpenDrawer(quote);
 for(const request of quoteRequests.slice(-2)) request.resolve({ok:true,json:async()=>[{ask:1300,bid:1303,age_seconds:0}]});
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(metric('买入 Ask'),'1,300');
 assert.equal(metric('整轮净价差'),'未知');
 assert.match(status().textContent,/等待机会列表同轮整轮估算/);
 // The initial async history response must retain frozen targets; a local
 // observations array cannot shadow the position object.
 context.window.echarts=echarts;
 context.window.openOpportunityHistoryDrawer({...position,entry_target:{target_bps:-25.84}});
 for(const request of otherRequests.slice(-3))request.resolve({ok:true,json:async()=>request.url.includes('/reversion-stats?')?{center_bps:10}:request.url.includes('/reference-history?')?{points:[]}:history});
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(chartOptions.series[0].markLine.data.find(item=>item.name==='本单冻结目标').yAxis,-25.84);
 // History requests follow the same canonical axis as execution even when
 // this opportunity buys B and sells A.
 vm.runInContext(fs.readFileSync('src/web/static/js/quote_freshness.js','utf8'),context);
 context.window.testOpenDrawer(channelRow);
 const chartRequests=otherRequests.slice(-3);
 assert.equal(new URL(chartRequests.find(r=>r.url.includes('/history?')).url).searchParams.get('pair'),'arcus->lighter');
 assert.equal(new URL(chartRequests.find(r=>r.url.includes('/reference-history?')).url).searchParams.get('buy_venue'),'arcus');
 context.window.testQuoteUpdate([{...channelRow,_receivedAt:now,_deliverySeconds:0}]);
 for(const request of chartRequests)request.resolve({ok:true,json:async()=>request.url.includes('/reversion-stats?')?{center_bps:999}:request.url.includes('/reference-history?')?{points:[]}:history});
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(chartOptions.series[0].markLine.data.find(item=>item.name==='下边界').yAxis,-150);
 assert.equal(chartOptions.series[0].markPoint.data[0].coord[1],(99-101)/101*10000);
 const livePoint=chartOptions.series[0].markPoint.data[0].coord;
 const liveTip=chartOptions.tooltip.formatter([{seriesIndex:0,value:livePoint}]);
 assert.match(liveTip,/固定 A\/B 价差/);
 assert.match(liveTip,/A买 \/ B卖/);
 assert.match(liveTip,/B买 \/ A卖/);
 const oldTip=chartOptions.tooltip.formatter([{seriesIndex:0,value:[livePoint[0]-1000,livePoint[1]]}]);
 assert.match(oldTip,/历史报价点/);
 assert.doesNotMatch(oldTip,/A买 \/ B卖：/,'current route estimates must not masquerade as historical returns');
 const currentChart=body.children.find(item=>item.className==='drawer-reversion-chart');
 assert.match(currentChart.text,/已触边；只读估算/);
 now+=6000;
 const staleTick=timers.at(-1)();
 assert.equal(chartOptions.series[0].markPoint,undefined,'expire the marker before a slow fallback quote request completes');
 assert.match(currentChart.text,/暂不判断触线/);
 for(const request of quoteRequests.slice(-2))request.resolve({ok:true,json:async()=>[{ask:100,bid:99,age_seconds:9,stale:true}]});
 await staleTick;
 context.window.testCloseDrawer();
 console.log('History drawer recovery, canonical channel bounds, frozen targets, live reuse, races and stale clearing: PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
