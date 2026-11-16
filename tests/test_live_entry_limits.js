'use strict';
const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
const nodes=new Map();
const node=id=>{
    if(!nodes.has(id)) nodes.set(id,{textContent:'',value:'single_position',setAttribute(){}});
    return nodes.get(id);
};
const context=vm.createContext({document:{getElementById:node,addEventListener(){}},console,
    fetch(){throw new Error('Entry limit status must reuse existing operations data');}});
let source=fs.readFileSync('src/web/static/js/operations.js','utf8');
source=source.replace(/\}\)\(\);\s*$/,
    `globalThis.renderLimits=(saved,execution,dirty=false)=>{
        savedEntryLimits=saved;strategyDirty=dirty;renderEntryLimits(execution);
    };globalThis.policyForTest=executionPolicy;
    globalThis.syncAmount=(saved,execution)=>{
        savedEntryLimits=saved;latestOperations={execution};syncExecutionCapLimit();
    };globalThis.scopeForTest=scopeKey;})();`);
vm.runInContext(source,context);
const saved={max_positions:2,max_total_position_notional:36,position_size:12};
const limits={...saved,version:1,saved_position_size:12,active_groups:3,single_position_cycle:false};
const execution={running:true,mode:'continuous_monitor',position_monitor:{entry_limits:limits}};
const text=()=>node('entry-limits-status').textContent;
context.renderLimits(saved,execution);
assert.match(text(),/本次生效：2 组.*当前 3 组/);
assert.doesNotMatch(text(),/等待下次|强平/);
context.renderLimits({...saved,max_positions:4},execution);
assert.match(text(),/本次生效：2 组.*已保存：4 组.*等待下次开仓检查应用/);
context.renderLimits(saved,{...execution,close_only:true});
assert.match(text(),/保存上限不会恢复开仓/);
context.renderLimits(saved,{...execution,stop_requested:true});
assert.match(text(),/保存上限不会恢复开仓/);
context.renderLimits(saved,{...execution,position_monitor:{...execution.position_monitor,heartbeat_stale:true}});
assert.match(text(),/上次上报（心跳待核验）/);
assert.doesNotMatch(text(),/本次生效/);
context.renderLimits(saved,{running:true});
assert.match(text(),/未上报动态上限.*本次执行结束后启动新版/);
context.renderLimits(saved,{running:false});
assert.match(text(),/下次启动使用/);
context.renderLimits(saved,execution,true);
assert.match(text(),/未保存修改/);
context.renderLimits(saved,{...execution,mode:'once'});
assert.match(text(),/单次验证.*不会.*追加组/);
context.renderLimits(saved,{...execution,position_monitor:{entry_limits:{...limits,
    position_size:8,execution_notional_cap:8,pending_other_settings:true}}});
assert.match(text(),/每腿 8 USDC.*启动上限 8 USDC.*其他策略修改待全部平仓/);
assert.doesNotMatch(text(),/等待下次开仓检查应用/,'a lower launch cap is not an unapplied save');
assert.equal(context.policyForTest().single_position_cycle,false,
    'an obsolete browser-restored single-cycle selection cannot override the group limit');
const template=fs.readFileSync('src/web/templates/index.html','utf8');
assert.doesNotMatch(template,/id="execution-cycle"/);
assert.equal((template.match(/id="entry-limits-status"/g)||[]).length,1);
node('execution-cap').value='12';node('execution-cap').max='12';
node('position-size').value='999'; // An unsaved draft must not raise authorization.
context.syncAmount(saved,{running:false});
assert.equal(node('execution-cap').value,'12');
assert.equal(node('execution-cap').disabled,true);
assert.equal(node('execution-cap-override').hidden,true);
assert.equal(context.policyForTest().follow_saved_cap,true);
context.syncAmount({...saved,position_size:20},{running:false});
assert.equal(node('execution-cap').value,'20','follow mode raises with an explicit saved edit');
const followedScope=context.scopeForTest();
node('execution-temporary-cap').checked=true;
context.syncAmount({...saved,position_size:20},{running:false});
assert.notEqual(context.scopeForTest(),followedScope,'same amount, different cap mode invalidates scope');
assert.equal(node('execution-cap').disabled,false);
node('execution-cap').value='8';
context.syncAmount({...saved,position_size:30},{running:false});
assert.equal(node('execution-cap').value,'8','temporary ceiling persists across saved increases');
context.syncAmount({...saved,position_size:5},{running:false});
assert.equal(node('execution-cap').value,'5','temporary amount still obeys saved decreases');
context.syncAmount(saved,{running:true,max_notional:8,position_monitor:{entry_limits:{...limits,
    execution_notional_cap:8,effective_entry_cap:6,limiting_sources:['总持仓剩余额度']}}});
assert.equal(node('execution-temporary-cap').checked,true,'legacy launch retains its ceiling');
assert.equal(node('execution-temporary-cap').disabled,true);
assert.match(node('execution-global-cap').textContent,/6 USDC.*总持仓剩余额度/);
context.syncAmount(saved,{running:true,follow_saved_cap:true,position_monitor:{entry_limits:{...limits,
    execution_notional_cap:null,effective_entry_cap:12,limiting_sources:['每腿设置']},heartbeat_stale:true}});
assert.equal(node('execution-temporary-cap').checked,false);
assert.match(node('execution-global-cap').textContent,/上次上报.*12 USDC/);
assert.match(template,/累计开仓目标（单腿名义金额）/);
console.log('Live group limits: applied/saved, lowering, stale worker, stop, launch cap and single-run state PASS; no requests');
