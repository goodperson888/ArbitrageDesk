'use strict';
const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
const nodes=new Map();
const node=id=>{
    if(!nodes.has(id)) nodes.set(id,{textContent:'',className:'',title:''});
    return nodes.get(id);
};
const context=vm.createContext({Date,console,
    document:{getElementById:node,addEventListener(){}},
    getSelectedVenues:()=>['lighter','arcus']
});
let source=fs.readFileSync('src/web/static/js/operations.js','utf8');
source=source.replace(/\}\)\(\);\s*$/,'globalThis.renderForTest=renderExecutionBanner;})();');
vm.runInContext(source,context);
function render(reason){
    const failure={event:'stage_failed',timestamp:new Date().toISOString(),detail:{
        symbol:'BOT',stage:'Lighter 主网 参考价格',reason}};
    context.renderForTest({running:true,state:'running',mode:'continuous_monitor',
        venues:['lighter','arcus'],latest_event:failure,reference_blocker:failure});
    return {heading:node('execution-live-title').textContent,
        detail:node('execution-live-detail').title,tone:node('execution-live-banner').className};
}
for(const reason of ['本地只读盘口 REST 共享节流；等待 2 秒（非平台限流）',
    'Lighter 盘口为空或缺少一侧；参考价探测冷却 30 秒',
    'Arcus 市场暂不可用；参考价探测冷却 30 秒']){
    const result=render(reason);
    assert.match(result.heading,/实盘扫描等待/);
    assert.match(result.tone,/waiting/);
    assert.match(result.detail,/其他品种继续扫描/);
    assert.doesNotMatch(result.detail,/HTTP 429/);
}
const limited=render('接口限流 HTTP 429；暂停 REST 请求 93 秒');
assert.match(limited.heading,/平台 REST 限流等待/);
assert.match(limited.detail,/93 秒/);
assert.match(limited.tone,/waiting/);
const failed=render('行情请求失败: HTTP 405');
assert.match(failed.heading,/实盘报价受阻/);
assert.match(failed.tone,/rejected/);
assert.match(failed.detail,/其他可用品种和平台组合继续扫描/);
context.renderForTest({running:false,state:'finished',tracked_position_count:1,
    note:'CASHCAT 已成交，自动平仓监控已停止',latest_event:{event:'stage_completed',
    timestamp:new Date(Date.now()-3600000).toISOString(),detail:{}}});
assert.match(node('execution-live-title').textContent,/已有成交持仓.*监控已停止/);
assert.match(node('execution-live-detail').title,/CASHCAT/);
const oldFailure={event:'stage_failed',timestamp:new Date(Date.now()-50000).toISOString(),
    detail:{stage:'Arcus 参考价格',reason:'接口限流 HTTP 429；暂停 REST 请求 121 秒'}};
context.renderForTest({running:true,mode:'continuous_monitor',venues:['lighter','arcus'],latest_event:oldFailure,
    last_rejection:oldFailure,reference_blocker:oldFailure,
    entry_scan_backoff_until:new Date(Date.now()+71000).toISOString()});
assert.match(node('execution-live-title').textContent,/新开仓扫描暂停/);
assert.match(node('execution-live-detail').title,/7[01] 秒/);
assert.doesNotMatch(node('execution-live-detail').title,/有效长连接行情仍可使用/);
context.renderForTest({running:true,mode:'continuous_monitor',venues:['lighter','arcus'],entry_scan:{last_preview_skip:{
    symbol:'SUI',route:'lighter->arcus',updated_at:new Date().toISOString(),
    optimistic_net_bps:-2,minimum_bps:8}}});
assert.match(node('execution-live-title').textContent,/盘口预判未达标/);
assert.match(node('execution-live-detail').title,/-2.00 \/ 门槛 8.00/);
context.renderForTest({running:true,mode:'continuous_monitor',venues:['lighter','arcus'],latest_event:{event:'planned',detail:{}},
    entry_scan:{last_preview_skip:{symbol:'SUI',updated_at:new Date().toISOString(),optimistic_net_bps:-2,minimum_bps:8}}});
assert.match(node('execution-live-title').textContent,/已达到入场条件/);
const hedge={event:'entry_cycle',timestamp:new Date().toISOString(),detail:{symbol:'BTC'}};
context.renderForTest({running:true,mode:'continuous_monitor',venues:['lighter','arcus'],single_position_cycle:false,latest_event:hedge});
assert.match(node('execution-live-detail').title,/依次开下一组/);
assert.doesNotMatch(node('execution-live-detail').title,/未自动重复开仓/);
context.renderForTest({running:true,mode:'continuous_monitor',venues:['lighter','arcus'],single_position_cycle:true,latest_event:hedge});
assert.match(node('execution-live-detail').title,/平仓并核对后/);
context.renderForTest({running:true,mode:'continuous_monitor',venues:['lighter','arcus'],single_position_cycle:false,latest_event:hedge,
    entry_scan:{capacity_block_reason:'已达同时持仓数上限'}});
assert.match(node('execution-live-detail').title,/已达同时持仓数上限/);
context.renderForTest({running:true,mode:'continuous_monitor',venues:['lighter','arcus'],single_position_cycle:false,
    entry_scan:{capacity_block_reason:'尚未达到交易间隔'}});
assert.match(node('execution-live-title').textContent,/尚未达到交易间隔/);
console.log('Discovery cooldown, local REST pacing and exchange 429 banners: PASS');
