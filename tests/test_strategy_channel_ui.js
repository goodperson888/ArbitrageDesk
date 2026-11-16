'use strict';
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const nodes=new Map(),requests=[],charts=[],timers=new Map();let ready,timer=0;
class Element {
    constructor(id){this.id=id;this.value='';this.disabled=false;this.hidden=false;this.open=false;this.listeners={};this.children=[];this.clientWidth=700;}
    addEventListener(name,fn){(this.listeners[name]??=[]).push(fn);}
    dispatchEvent(e){for(const fn of this.listeners[e.type]||[])fn(e);}
    click(){if(!this.disabled)this.dispatchEvent({type:'click'});}
    querySelectorAll(){return [...nodes].filter(([id])=>/^channel-(method|window|quantile|multiplier|min-samples|min-width|max-width|lower|upper|exit-mode|reversion)$/.test(id)).map(([,n])=>n);}
    showModal(){this.open=true;}
    close(){this.open=false;this.dispatchEvent({type:'close'});}
    focus(){} setAttribute(){} reportValidity(){return true;}
    replaceChildren(){this.children=[];} appendChild(child){this.children.push(child);}
}
const get=id=>{if(!nodes.has(id))nodes.set(id,new Element(id));return nodes.get(id);};
const context=vm.createContext({document:{getElementById:get,querySelectorAll:()=>[],querySelector:()=>get('controls'),createElement:()=>new Element(),
    addEventListener:(name,fn)=>{if(name==='DOMContentLoaded')ready=fn;},activeElement:new Element()},
    window:{addEventListener(){},getSelectedVenues:()=>['arcus','lighter'],getExecutionSelection:()=>({symbols:['SUI']}),
        echarts:{init:()=>({setOption:option=>charts.push(option),resize(){}})}},
    fetch:(url,options)=>new Promise(resolve=>requests.push({url,body:JSON.parse(options.body),resolve})),
    AbortController,Event:class {constructor(type){this.type=type;}},
    setTimeout:fn=>{timers.set(++timer,fn);return timer;},clearTimeout:id=>timers.delete(id),console});
vm.runInContext(fs.readFileSync('src/web/static/js/strategy_preview.js','utf8'),context);ready();
const ui=context.window.StrategyChannelUI;
const saved={entry_policy:{mode:'fixed'},channel_policy:{method:'quantile'},close_policy:{min_profit_usdc:.01}};
get('execution-entry-mode').value='fixed';get('position-mode').value='single';ui.load(saved);
const respond=(r,extra={})=>r.resolve({ok:true,json:async()=>({bounds:{lower_bps:-20,upper_bps:20,center_bps:0,width_bps:40},points:[],positions:[],...extra})});
const settle=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
    let saves=0;context.window.saveStrategyConfig=async()=>{saves++;return {success:false};};
    ui.open(true);get('channel-method').value='robust';get('strategy-preview-close').click();
    assert.equal(ui.read().method,'quantile');assert.equal(saves,0,'cancel never persists or starts execution');
    assert.equal(get('execution-entry-mode').value,'fixed');
    ui.open(false);assert.equal(get('channel-method').disabled,true);
    assert.equal(get('controls').hidden,true,'read-only view uses the diagram instead of disabled configuration fields');
    get('strategy-preview-close').click();
    assert.equal(requests.length,0,'global settings and saved-strategy diagrams never load a symbol-specific history');
    ui.open(true);get('channel-method').value='fixed';get('channel-upper').value=8;get('channel-lower').value=-8;
    get('channel-settings-form').dispatchEvent({type:'input'});
    for(const fn of timers.values())fn();timers.clear();
    assert.equal(requests.length,0,'editing a global diagram adds no historical requests');
    assert.equal(charts.at(-1).series[0].markLine.data.find(x=>x.name==='上边界').yAxis,8,'parameter changes still redraw the diagram');
    assert.match(get('strategy-preview-axis').textContent,/全局策略/);
    get('channel-settings-save').click();await settle();
    assert.equal(saves,1);assert.equal(get('execution-entry-mode').value,'fixed','failed save restores entry mode');
    assert.equal(get('strategy-preview-dialog').open,true);assert.equal(get('channel-settings-save').disabled,false);
    let finish;context.window.saveStrategyConfig=()=>new Promise(resolve=>{saves++;finish=resolve;});
    get('channel-settings-save').click();get('channel-settings-save').click();get('strategy-preview-close').click();
    assert.equal(saves,2,'duplicate click cannot issue a second save');assert.equal(get('strategy-preview-dialog').open,true);
    const p=ui.read();ui.load({...saved,entry_policy:{mode:'channel'},channel_policy:p});finish({success:true});await settle();
    assert.equal(get('strategy-preview-dialog').open,false);assert.equal(ui.basis().mode,'channel');
    const frozen={symbol:'UNI',status:'open',entry_target:{channel:{method:'fixed',policy:{method:'fixed'},axis_venues:['arcus','lighter'],
        lower_bps:-20,upper_bps:20,center_bps:0,entry_axis_bps:35,exit_axis_bps:14,close_policy:{min_profit_usdc:.02}}}};
    ui.open(false,frozen);respond(requests.at(-1));await settle();
    assert.equal(requests.at(-1).body.symbol,'UNI');assert.deepEqual(requests.at(-1).body.venues,['arcus','lighter']);
    assert.match(get('strategy-preview-axis').textContent,/UNI.*本单历史与冻结目标/);
    assert.equal(charts.at(-1).series[0].markLine.data.find(x=>x.name==='本单冻结平仓目标').yAxis,14);
    assert.match(get('strategy-preview-description').textContent,/0.02 USDC/);
    get('strategy-preview-close').click();assert.equal(ui.basis().mode,'channel','viewing an old position cannot change saved strategy');
    ui.open(false,frozen);const old=requests.at(-1);get('strategy-preview-close').click();
    ui.open(false);const option=charts.at(-1);respond(old);await settle();
    assert.equal(charts.at(-1),option,'late per-order history cannot replace a global diagram');
    assert.equal(charts.at(-1).xAxis.type,'category','global preview always returns to rule illustration');
    assert.match(get('strategy-preview-axis').textContent,/全局策略/);
    get('strategy-preview-close').click();
    console.log('Channel UI: global diagrams without history requests, staged save, frozen-order history and stale response PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
