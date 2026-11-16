'use strict';
const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
class Element {
    constructor(){this.children=[];this.listeners={};this.dataset={};this.textContent='';}
    appendChild(child){this.children.push(child);}
    addEventListener(name,fn){this.listeners[name]=fn;}
    setAttribute(name,value){this[name]=value;}
}
const notice=new Element(), confirmations=[], requests=[], timers=new Map();
let approved=true, nextResponse, refreshes=0;
const context=vm.createContext({document:{createElement:()=>new Element(),getElementById:()=>notice,
    querySelector:()=>({content:'test-local-token'})},AbortController,
    confirm:message=>{confirmations.push(message);return approved;},
    fetch:(path,options)=>{requests.push({path,options});return new Promise(resolve=>{nextResponse=resolve;});},
    setTimeout:fn=>{const key=timers.size+1;timers.set(key,fn);return key;},clearTimeout:key=>timers.delete(key),
    loadExecutionHistory:async()=>{refreshes++;},console});
vm.runInContext(fs.readFileSync('src/web/static/js/manual_close.js','utf8'),context);
const position={id:'one-group',symbol:'SUI',variational_venue:'lighter',lighter_venue:'arcus',
    variational_size:2,lighter_size:2,manual_close_control:{available:true,reason:'本组双平'}};
let stopped=0;
const event={stopPropagation(){stopped++;}};
const action=p=>context.appendManualCloseAction(p,new Element());
(async()=>{
    let button=action({...position,manual_close_control:{available:false,reason:'未知订单待核对'}});
    assert.equal(button.disabled,true);assert.match(button.title,/未知订单/);
    await button.listeners.click(event);assert.equal(requests.length,0);
    button=action(position);approved=false;
    await button.listeners.click(event);assert.equal(requests.length,0,'cancel never queues an exit');
    approved=true;
    const first=button.listeners.click(event);
    assert.equal(button.textContent,'提交中');assert.equal(button.disabled,true);
    assert.match(confirmations.at(-1),/可能亏损/);assert.match(confirmations.at(-1),/剩余数量：2 \/ 2/);
    assert.match(confirmations.at(-1),/全局新开仓状态保持不变/);
    assert.equal(requests.length,1);
    assert.equal(requests[0].path,'/api/execution/manual-close');
    assert.equal(requests[0].options.headers['X-Local-Token'],'test-local-token');
    assert.deepEqual(JSON.parse(requests[0].options.body),{position_id:'one-group',symbol:'SUI',venues:['lighter','arcus']});
    const refreshed=action(position);
    assert.equal(refreshed.disabled,true,'history refresh cannot bypass an in-flight request');
    await button.listeners.click(event);assert.equal(requests.length,1);
    nextResponse({ok:true,json:async()=>({success:true,orders_submitted:false,message:'双平已排队，等待回执'})});
    await first;
    assert.equal(button.textContent,'双平待确认');assert.match(notice.textContent,/等待回执/);
    assert.doesNotMatch(notice.textContent,/平仓成功/);assert.equal(refreshes,1);assert.equal(timers.size,0);
    assert.equal(action({...position,manual_close_control:{available:false,pending:true}}).textContent,'双平待确认');
    button=action(position);const failed=button.listeners.click(event);
    nextResponse({ok:false,json:async()=>({success:false,message:'本组当前待核对'})});await failed;
    assert.match(notice.textContent,/本组当前待核对/);assert.equal(notice.dataset.failed,'true');
    assert.equal(refreshes,2);
    const before=stopped;button.listeners.keydown(event);assert.equal(stopped,before+1,'button keyboard event never opens the row drawer');
    assert.ok(stopped>=5,'clicks cannot bubble into the history drawer');
    console.log('Manual close UI: confirmation, exact group, singleflight, queued feedback and failure refresh PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
