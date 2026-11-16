'use strict';
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
class Element {
    constructor() { this.textContent = ''; this.listeners = {}; this.classes = new Set(); this.classList = {toggle:(name,on)=>on?this.classes.add(name):this.classes.delete(name)}; }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    getAttribute() { return '#execution-history-panel'; }
}
const nodes = new Map(['desk-position-status','desk-run','desk-active-scope','desk-scope-symbols','execution-history-panel'].map(id => [id,new Element()]));
let now = 100000, ready;
const context = vm.createContext({document:{getElementById:id=>nodes.get(id),querySelectorAll:()=>[],addEventListener:(event,callback)=>{ready=callback;}},
    Date:class extends Date { static now(){return now;} },console,
    fetch(){throw new Error('Presentation must not issue requests');},setInterval(){throw new Error('Presentation must not add polling');}});
const dashboard = fs.readFileSync('src/web/static/js/dashboard.js','utf8');
vm.runInContext(dashboard,context);
ready();
const position = {id:'a',symbol:'SUI',status:'open'};
const live = {id:'a',account_status:'ready',last_success_at:new Date(now).toISOString()};
const monitor = {running:true,updated_at:new Date(now).toISOString(),positions:[live]};
const link = nodes.get('desk-position-status');
context.updateDeskPositions({scope:'all',open_positions:[position]});
assert.match(link.textContent,/持仓 1 组 · 等待监控核验/);
context.updateDeskMonitor({monitor,position_count:1});
assert.equal(link.textContent,'持仓 1 组 · 监控中');
now += 6000;
context.updateDeskMonitor({monitor,position_count:1});
assert.match(link.textContent,/心跳待核验/);
assert(link.classes.has('attention'));
now = 100000;
context.updateDeskMonitor({monitor:{...monitor,positions:[{...live,error:'account unavailable'}]},position_count:1});
assert.match(link.textContent,/监控待核对/);
context.updateDeskMonitor({monitor:{...monitor,running:false},position_count:1});
assert.match(link.textContent,/监控已停止/);
context.updateDeskMonitor({error:'监控状态流重连中'});
assert.match(link.textContent,/状态重连中/);
// A filtered history response must not hide another platform's live position.
context.updateDeskPositions({scope:'pair',open_positions:[{id:'b',symbol:'UNI'}]});
assert.match(link.textContent,/持仓 2 组/);
context.updateDeskPositions({scope:'pair',closed_positions:[{id:'b'}],open_positions:[]});
assert.match(link.textContent,/持仓 1 组/);
context.updateDeskPositions({scope:'all',open_positions:[]});
assert.equal(link.textContent,'持仓 0 组');
// The compact entry uses the existing loader and clears history-only filters.
const app = fs.readFileSync('src/web/static/js/app.js','utf8');
const navigation = app.slice(app.indexOf('globalThis.showOpenExecutionHistory ='),app.indexOf('async function loadExecutionHistory(){'));
let requests=0, focused=false, scrolled=false, prevented=false;
const panel = nodes.get('execution-history-panel');
panel.scrollIntoView=()=>{scrolled=true;}; panel.querySelector=()=>({focus:()=>{focused=true;}});
context.loadExecutionHistory=()=>{requests++;};
vm.runInContext("let executionHistoryView='closed', executionHistoryPair='lighter,arcus', executionHistoryPage=4; const $=id=>document.getElementById(id);"+navigation,context);
link.listeners.click({preventDefault(){prevented=true;}});
assert.equal(vm.runInContext('executionHistoryView',context),'open');
assert.equal(vm.runInContext('executionHistoryPair',context),'');
assert.equal(vm.runInContext('executionHistoryPage',context),1);
assert.equal(requests,1); assert(panel.open && focused && scrolled && prevented);
const template=fs.readFileSync('src/web/templates/index.html','utf8');
assert.doesNotMatch(template,/id="desk-positions"|id="desk-position-list"/);
assert.match(template,/id="desk-position-status"/);
console.log('Dashboard: compact status, monitor failures, independent history scope and open-order navigation PASS; no extra polling');
