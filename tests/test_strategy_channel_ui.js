'use strict';
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const nodes=new Map(),requests=[],charts=[],timers=new Map();let ready,timer=0;
class Element {
    constructor(id){this.id=id;this.value='';this.disabled=false;this.hidden=false;this.open=false;this.listeners={};this.children=[];this.clientWidth=700;this.style={};this.attributes={};this.dataset={tooltip:'仅问号显示的说明'};}
    addEventListener(name,fn){(this.listeners[name]??=[]).push(fn);}
    dispatchEvent(e){for(const fn of this.listeners[e.type]||[])fn(e);}
    click(){if(!this.disabled)this.dispatchEvent({type:'click'});}
    querySelectorAll(selector){if(selector==='.field-help-icon')return [get('help-method'),get('help-window')];return [...nodes].filter(([id])=>/^channel-(method|window|quantile|multiplier|min-samples|min-width|max-width|lower|upper|exit-mode|reversion|touch-tolerance|touch-count)$/.test(id)).map(([,n])=>n);}
    showModal(){this.open=true;}
    close(){this.open=false;this.dispatchEvent({type:'close'});}
    focus(){} setAttribute(k,v){this.attributes[k]=v;} removeAttribute(k){delete this.attributes[k];} reportValidity(){return true;}
    matches(){return Boolean(this.keyboardFocus);}
    getBoundingClientRect(){return this.id==='strategy-preview-dialog'?{left:16,right:374,top:16,bottom:828}:
        this.id?.startsWith('help-')?{left:350,right:374,top:760,bottom:784}:{width:280,height:120};}
    replaceChildren(){this.children=[];} appendChild(child){this.children.push(child);}
}
const get=id=>{if(!nodes.has(id))nodes.set(id,new Element(id));return nodes.get(id);};
const context=vm.createContext({document:{getElementById:get,querySelectorAll:()=>[],querySelector:()=>get('controls'),createElement:()=>new Element(),
    addEventListener:(name,fn)=>{if(name==='DOMContentLoaded')ready=fn;},activeElement:new Element()},
    window:{innerWidth:390,innerHeight:844,addEventListener(){},getSelectedVenues:()=>['arcus','lighter'],getExecutionSelection:()=>({symbols:['SUI']}),
        echarts:{init:()=>({setOption:option=>charts.push(option),resize(){}})}},
    fetch:(url,options)=>new Promise(resolve=>requests.push({url,body:JSON.parse(options.body),resolve})),
    AbortController,Event:class {constructor(type){this.type=type;}},
    setTimeout:fn=>{timers.set(++timer,fn);return timer;},clearTimeout:id=>timers.delete(id),console});
vm.runInContext(fs.readFileSync('src/web/static/js/strategy_preview.js','utf8'),context);ready();
const help=get('strategy-preview-dialog').children.find(node=>node.id==='channel-field-help');
const helpButton=get('help-method'),otherHelp=get('help-window');
assert.equal(help.hidden,true);
helpButton.dispatchEvent({type:'mouseenter'});
assert.equal(help.hidden,false);
assert.equal(help.style.left,'86px','tooltip stays inside the right edge of the dialog');
assert.equal(help.style.top,'633px','tooltip flips above when space below is insufficient');
helpButton.dispatchEvent({type:'click',preventDefault(){},stopPropagation(){}});
helpButton.dispatchEvent({type:'mouseleave'});
assert.equal(help.hidden,true,'clicks must not leave the tooltip pinned after leaving the question mark');
helpButton.dispatchEvent({type:'focus'});
assert.equal(help.hidden,true,'mouse focus must not show or restore the tooltip');
helpButton.keyboardFocus=true;helpButton.dispatchEvent({type:'focus'});
assert.equal(help.hidden,false,'keyboard focus on the question mark retains accessible help');
helpButton.dispatchEvent({type:'keydown',key:'Escape',preventDefault(){},stopPropagation(){}});
assert.equal(help.hidden,true);
otherHelp.dispatchEvent({type:'mouseenter'});
assert.equal(helpButton.attributes['aria-expanded'],'false');
assert.equal(otherHelp.attributes['aria-expanded'],'true');
get('strategy-preview-dialog').dispatchEvent({type:'scroll'});
assert.equal(help.hidden,true,'scrolling cannot leave a tooltip at stale coordinates');
const template=fs.readFileSync('src/web/templates/index.html','utf8');
for(const id of ['channel-method','channel-window','channel-exit-mode'])assert.match(template,new RegExp('<label for="'+id+'"'),
    'clicking field text must target its select, rather than the nested question mark');
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
    ui.open(true);get('channel-method').value='touches';get('channel-touch-tolerance').value=1.5;get('channel-touch-count').value=4;
    assert.equal(ui.read().method,'touches');assert.equal(ui.read().touch_tolerance_bps,1.5);assert.equal(ui.read().touch_min_count,4);
    get('strategy-preview-close').click();assert.equal(ui.read().method,'quantile','cancel restores the selected method');
    assert.equal(ui.read().touch_min_count,3,'cancel restores touch settings');
    ui.open(false);assert.equal(get('channel-method').disabled,true);
    assert.equal(get('channel-touch-count').disabled,true);
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
    const touches={upper:{count:4,low_bps:19,high_bps:21,points:[{timestamp:100,value_bps:20}]},
        lower:{count:3,low_bps:-21,high_bps:-19,points:[{timestamp:110,value_bps:-20}]}};
    frozen.entry_target.channel.touch_statistics=touches;
    get('strategy-preview-close').click();ui.open(false,frozen);respond(requests.at(-1));await settle();
    assert.equal(charts.at(-1).series[0].markArea.data.length,2,'frozen touch zones are drawn');
    get('strategy-preview-close').click();assert.equal(ui.basis().mode,'channel','viewing an old position cannot change saved strategy');
    ui.open(false,frozen);const old=requests.at(-1);get('strategy-preview-close').click();
    ui.open(false);const option=charts.at(-1);respond(old);await settle();
    assert.equal(charts.at(-1),option,'late per-order history cannot replace a global diagram');
    assert.equal(charts.at(-1).xAxis.type,'category','global preview always returns to rule illustration');
    assert.match(get('strategy-preview-axis').textContent,/全局策略/);
    get('strategy-preview-close').click();
    console.log('Channel UI: global diagrams without history requests, staged save, frozen-order history and stale response PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
