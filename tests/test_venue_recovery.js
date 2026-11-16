'use strict';
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
let now=100000, timerId=0, releaseProbe;
const timers=new Map(),requests=[],sockets=[];
class Socket {
    constructor(url){this.url=url;this.readyState=0;sockets.push(this);}
    close(){this.readyState=3;}
}
const context=vm.createContext({
    window:{},document:{getElementById:()=>null,querySelectorAll:()=>[],addEventListener(){}},
    localStorage:{getItem:()=>null},Date:class extends Date{static now(){return now;}},
    AbortController,console,location:{protocol:'http:'},WebSocket:Socket,
    setTimeout(fn,ms){const id=++timerId;timers.set(id,{fn,ms});return id;},
    clearTimeout(id){timers.delete(id);},
    fetch:async url=>{requests.push(url);return {ok:true,json:()=>new Promise(resolve=>{releaseProbe=resolve;})};}
});
vm.runInContext(fs.readFileSync('src/web/static/js/quote_freshness.js','utf8'),context);
const source=fs.readFileSync('src/web/static/js/cross_venue.js','utf8');
vm.runInContext(source.replace(/\}\)\(\);\s*$/,
    'window.test={state,venueHealth,recoverVenueHealth,recoverOpportunityStream,startOpportunityStream,closeOpportunityStream,updateVenueDiagnosticsFromRows};})();'),context);
const {state,venueHealth,recoverVenueHealth,recoverOpportunityStream,startOpportunityStream,
    closeOpportunityStream,updateVenueDiagnosticsFromRows}=context.window.test;
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const quote=(changes={})=>({bid:100,ask:101,stale:false,age_seconds:.5,
    fetched_at:new Date(now-500).toISOString(),source:'websocket',...changes});
state.selectedVenues=['lighter','arcus'];
state.instruments={lighter:[{canonical_symbol:'SOL-PERP'}],arcus:[{canonical_symbol:'SOL-PERP'}]};
state.venueDiagnostics={lighter:{ok:false,detail:'网络错误',checkedAt:now-31000}};
(async()=>{
    recoverVenueHealth();recoverVenueHealth();await tick();
    assert.equal(requests.length,1,'repeated UI ticks share the in-flight recovery probe');
    assert.match(requests[0],/symbol=SOL&venue=lighter/,'probe a supported market instead of assuming BTC exists');
    releaseProbe([quote()]);await tick();
    assert.equal(venueHealth('lighter').tone,'ok');
    recoverVenueHealth();assert.equal(requests.length,1,'healthy venues add no REST traffic');
    assert.equal(state.venueDiagnostics.lighter.quoteAt,now-500,'probe age is retained');

    now+=31000;
    recoverVenueHealth();await tick();
    releaseProbe([{stale:true,reason:'接口限流 HTTP 429；暂停 REST 请求 93 秒'}]);await tick();
    const cooldown=state.venueDiagnostics.lighter.retryUntil;
    now=cooldown-1;recoverVenueHealth();
    assert.equal(requests.length,2,'automatic recovery obeys the full exchange cooldown');
    now=cooldown;recoverVenueHealth();await tick();
    assert.equal(requests.length,3);
    releaseProbe([{stale:true,reason:'行情请求超时'}]);await tick();
    assert.equal(state.venueProbesPending,0,'background recovery does not perform three immediate timeout retries');
    recoverVenueHealth();assert.equal(requests.length,3,'failed recovery backs off');

    now+=61000;recoverVenueHealth();await tick();
    updateVenueDiagnosticsFromRows([{canonical_symbol:'SOL-PERP',buy_venue:'lighter',sell_venue:'arcus',
        buy_price:100,sell_price:101,buy_stale:false,sell_stale:false,buy_age_seconds:0,sell_age_seconds:0}]);
    releaseProbe([{stale:true,reason:'较早发起的 REST 请求失败'}]);await tick();
    assert.equal(venueHealth('lighter').tone,'ok','fresh stream data wins over a late failed recovery');
    assert.equal(state.venueRecovery.lighter,undefined);

    now+=31000;recoverVenueHealth();await tick();
    releaseProbe([quote({fetched_at:new Date(now-30000).toISOString()})]);await tick();
    assert.notEqual(venueHealth('lighter').tone,'ok','an old timestamp cannot be repainted green');
    assert.match(venueHealth('lighter').detail,/已过期/);

    state.auto=false;state.liveStreamWanted=false;now+=61000;
    const pausedCalls=requests.length;recoverVenueHealth();
    assert.equal(requests.length,pausedCalls,'a fully paused scanner does not launch probes');
    state.venueDiagnostics={};state.liveSymbols=['SOL'];
    startOpportunityStream();
    const first=sockets[0];state.loading=true;now+=20001;recoverOpportunityStream();
    assert.equal(first.readyState,3,'a hung connection attempt is closed');
    const reconnect=timers.get(state.liveReconnectTimer);
    assert.equal(reconnect.ms,1000);reconnect.fn();
    const second=sockets[1];assert.ok(second,'REST scanning must not delay websocket recovery');state.loading=false;
    second.readyState=1;second.onopen();
    now+=25000;second.onmessage({data:JSON.stringify({rows:[],heartbeat:true})});
    assert.equal(state.venueDiagnostics.lighter,undefined,'transport heartbeats never create quote evidence');
    now+=10000;recoverOpportunityStream();
    assert.equal(second.readyState,1,'received heartbeats keep an idle quote stream connected');
    now+=20001;recoverOpportunityStream();
    assert.equal(second.readyState,3,'an open socket with no frames is recovered');
    const scheduled=timers.get(state.liveReconnectTimer);
    const late=second.onmessage;
    scheduled.fn();const third=sockets[2];
    const before=state.liveLastMessageAt;
    late({data:JSON.stringify({rows:[],heartbeat:true})});
    assert.equal(state.liveSocket,third,'late messages cannot restore a retired connection');
    assert.equal(state.liveLastMessageAt,before);
    closeOpportunityStream();recoverOpportunityStream();
    assert.equal(state.liveReconnectTimer,null,'intentional disconnect cancels automatic reconnection');
    assert.equal(third.readyState,3);
    console.log('Bounded venue recovery, cooldown, timestamp safety and silent websocket reconnection: PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
