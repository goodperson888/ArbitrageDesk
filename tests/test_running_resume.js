'use strict';
const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
const nodes=new Map(), requests=[];
const node=id=>{
    if(!nodes.has(id)) nodes.set(id,{value:'12',textContent:'',disabled:false,hidden:false,
        setAttribute(){},replaceChildren(){},appendChild(){}});
    return nodes.get(id);
};
let execution, reply={success:true,message:'已恢复'}, fail=false, dirty=false;
const operations=()=>({execution,lighter:{},bridge:{symbols:[]},feeds:{variational:{},lighter:{}},
    capture:{position:{known:false},chains:[],warnings:[],evidence:[]},diagnostics:[]});
const context=vm.createContext({document:{getElementById:node,querySelector:()=>({content:'test-token'}),
        createElement:()=>({}),addEventListener(){}},console,Date,AbortController,setTimeout,clearTimeout,URLSearchParams,
    // Current scanner selection is deliberately different from the active run.
    getSelectedVenues:()=>['lighter','standx'],getExecutionSelection:()=>({mode:'selected',symbols:['BTC']}),
    fetch:async(path,options)=>{
        requests.push({path,options});
        if(path==='/api/execution/resume'){
            if(fail) return {ok:false,status:409,text:async()=>JSON.stringify({error:'停止请求已变化'})};
            execution={...execution,stop_requested:false};
            return {ok:true,json:async()=>reply};
        }
        return {ok:true,json:async()=>operations()};
    }});
let source=fs.readFileSync('src/web/static/js/operations.js','utf8');
source=source.replace(/\}\)\(\);\s*$/,
    `globalThis.renderForTest=(data,dirty=false)=>{latestOperations={execution:data};strategyDirty=dirty;renderResumeControl();};
    globalThis.resumeForTest=resumeEntries;})();`);
vm.runInContext(source,context);
const active={running:true,stop_requested:true,mode:'continuous_monitor',close_only:false,
    venues:['lighter','lighter_rh','arcus'],pid:123,started_at:'run1',
    resume_control:{available:true,pid:123,started_at:'run1',stop_generation:'7:10'}};
(async()=>{
    execution={...active};
    context.renderForTest(execution);
    assert.equal(node('execution-resume').hidden,false);
    assert.equal(node('execution-resume').disabled,false);
    assert.match(node('execution-resume').title,/原运行范围/);
    assert.equal(requests.length,0,'rendering must never resume automatically');
    await context.resumeForTest();
    const resume=requests.find(r=>r.path==='/api/execution/resume');
    assert.deepEqual(JSON.parse(resume.options.body),{pid:123,started_at:'run1',stop_generation:'7:10'});
    assert.equal(resume.options.headers['X-Local-Token'],'test-token');
    assert.equal(node('execution-resume').hidden,true,'a confirmed resume removes the draining action');
    assert.equal(requests.some(r=>/\/(monitor|once|wait-once|reset-stop)$/.test(r.path)),false);

    execution={...active};fail=true;context.renderForTest(execution);
    await context.resumeForTest();
    assert.match(node('execution-check-result').textContent,/停止请求已变化/);
    assert.equal(node('execution-resume').disabled,false,'failed requests must leave a retryable control');
    context.renderForTest(execution,true);
    assert.equal(node('execution-resume').disabled,true);
    const count=requests.length;
    await context.resumeForTest();
    assert.equal(requests.length,count,'unsaved edits cannot submit resume');
    for(const delta of [{running:false},{close_only:true},{mode:'once'},{stop_requested:false}]){
        context.renderForTest({...active,...delta});
        assert.equal(node('execution-resume').hidden,true);
    }
    context.renderForTest({...active,resume_control:{available:false,reason:'原仓位待核对'}});
    assert.equal(node('execution-resume').disabled,true);
    assert.match(node('execution-resume').title,/原仓位待核对/);
    console.log('Running resume: explicit click, original run binding, no relaunch, failure and dirty-state controls PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
