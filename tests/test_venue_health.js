'use strict';
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
let now=100000,releaseProbe;
const nodes=new Map(),requests=[];
function element(){
    return {children:[],dataset:{},className:'',appendChild(child){this.children.push(child);},
        append(...children){this.children.push(...children);},replaceChildren(){this.children=[];},setAttribute(){},
        querySelector(selector){return selector==='input'?this.children.find(child=>child.type==='checkbox'):null;},
        classList:{toggle(){}},addEventListener(){}};
}
const picker=element();nodes.set('v2-venue-select',picker);
const context=vm.createContext({
    window:{},Date:class extends Date{static now(){return now;}},
    document:{getElementById:id=>nodes.get(id),createElement:element,addEventListener(){},
        querySelectorAll:selector=>selector==='#v2-venue-select .venue-option'?picker.children:[]},
    localStorage:{getItem:()=>null},AbortController,setTimeout,clearTimeout,console,
    fetch:async url=>{requests.push(url);return {ok:true,json:async()=>await new Promise(resolve=>{releaseProbe=resolve;})};}
});
vm.runInContext(fs.readFileSync('src/web/static/js/quote_freshness.js','utf8'),context);
let source=fs.readFileSync('src/web/static/js/cross_venue.js','utf8');
source=source.replace(/\}\)\(\);\s*$/,'window.test={state,venueHealth,updateVenueDiagnosticsFromRows,probeSelectedVenues,fillVenuePicker};})();');
vm.runInContext(source,context);
const {state,venueHealth,updateVenueDiagnosticsFromRows,probeSelectedVenues,fillVenuePicker}=context.window.test;
state.venues=[{venue:'lighter',status:'degraded',last_error:'只读盘口 REST 共享节流；等待 2 秒'}];
state.selectedVenues=['lighter','arcus'];
const row=(changes={})=>({canonical_symbol:'SUI-PERP',buy_venue:'lighter',sell_venue:'arcus',
    buy_price:1,sell_price:1.01,buy_age_seconds:0.2,sell_age_seconds:0.3,buy_stale:false,sell_stale:false,
    buy_source:'lighter_websocket',sell_source:'arcus_websocket',_receivedAt:now,_deliverySeconds:0,...changes});
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
    assert.equal(venueHealth('lighter').tone,'waiting','local REST pacing is not a disconnected venue');
    state.venueDiagnostics.lighter={ok:false,detail:'只读盘口 REST 共享节流；等待 2 秒',checkedAt:now,retryUntil:now+2000};
    fillVenuePicker(picker,['lighter'],x=>x,['lighter']);
    assert.match(picker.children[0].className,/is-waiting/);
    assert.doesNotMatch(picker.children[0].className,/is-error/);
    now+=2100;
    assert.match(venueHealth('lighter').detail,/等待已结束/);
    updateVenueDiagnosticsFromRows([row()]);
    assert.equal(venueHealth('lighter').tone,'ok');
    assert.match(venueHealth('lighter').detail,/SUI.*lighter_websocket/);
    assert.equal(requests.length,0,'live recovery never requests a diagnostic or REST quote');
    const quoteAt=state.venueDiagnostics.lighter.quoteAt;
    now+=11000;
    updateVenueDiagnosticsFromRows([]);
    assert.equal(state.venueDiagnostics.lighter.quoteAt,quoteAt,'heartbeat cannot renew quote freshness');
    assert.equal(venueHealth('lighter').tone,'waiting');
    assert.match(venueHealth('lighter').detail,/行情已过期/);
    state.venueDiagnostics.lighter={ok:false,detail:'BTC 报价请求失败：网络错误',checkedAt:now};
    assert.equal(venueHealth('lighter').tone,'error');
    for(const changes of [{buy_stale:true},{buy_age_seconds:11},{buy_price:0},
        {buy_price:NaN},{buy_age_seconds:null},{_deliverySeconds:11}]){
        updateVenueDiagnosticsFromRows([row(changes)]);
        assert.equal(venueHealth('lighter').tone,'error','unusable quotes never hide a real error');
    }
    now+=31000;
    assert.equal(venueHealth('lighter').tone,'waiting','an old failed probe is not permanent evidence of disconnection');
    state.venueDiagnostics.lighter={ok:false,detail:'接口限流 HTTP 429；暂停 REST 请求 93 秒',checkedAt:now,retryUntil:now+93000};
    assert.equal(venueHealth('lighter').tone,'waiting');
    assert.match(venueHealth('lighter').detail,/HTTP 429/,'real exchange cooldown remains explicit');
    const pending=probeSelectedVenues(['lighter']);await tick();
    now+=10;updateVenueDiagnosticsFromRows([row()]);
    releaseProbe([{bid:null,ask:null,stale:true,reason:'只读盘口 REST 共享节流；等待 2 秒'}]);
    await pending;
    assert.equal(venueHealth('lighter').tone,'ok','a late failed BTC probe cannot overwrite a fresh stream quote');
    assert.equal(requests.length,1,'normal recovery adds zero requests beyond the original one-time probe');
    state.selectedVenues=['arcus'];
    delete state.venueDiagnostics.lighter;
    updateVenueDiagnosticsFromRows([row()]);
    assert.equal(state.venueDiagnostics.lighter,undefined,'unselected venue evidence cannot overwrite current scope');
    console.log('Venue pacing warning, quote-driven recovery, stale aging and probe races: PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
