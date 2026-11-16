'use strict';
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
let now=100000,init,responseMode='normal';const nodes=new Map(),timers=[],requests=[],events=new Map();
function node(id){if(!nodes.has(id))nodes.set(id,{id,value:id==='execution-symbol'?'ETH':'12',textContent:'',disabled:false,checked:false,setAttribute(){},before(child){nodes.set(child.id,child);},replaceChildren(){},appendChild(){}});return nodes.get(id);}
const context=vm.createContext({
 document:{getElementById:node,createElement:()=>({setAttribute(){}}),querySelector:()=>({content:'test'}),addEventListener:(_,fn)=>{init=fn;},hidden:false},
 Date:class extends Date{static now(){return now;}},AbortController,setTimeout,clearTimeout,setInterval:fn=>timers.push(fn),
 addEventListener:(name,handler)=>events.set(name,handler),
 URLSearchParams,
 getSelectedVenues:()=>['lighter','arcus'],
 getExecutionSelection:()=>({mode:'selected',symbols:['ETH']}),
 fetch:async(path,options)=>{requests.push(path);if(responseMode==='html'&&path.endsWith('/check'))return {ok:true,status:200,headers:{get:()=> 'text/html'},text:async()=> '<!doctype html><html><body>wrong service</body></html>'};if(responseMode==='blocked'&&path.endsWith('/check'))return {ok:true,status:200,headers:{get:()=> 'application/json'},text:async()=> JSON.stringify({ready:false,blockers:['Lighter 费率证据过期']})};return {ok:true,json:async()=>path.endsWith('/wait-once')?{started:true,message:'自动等待已启动'}:path.endsWith('/reset-stop')?{success:true,orders_submitted:false,message:'已解除停止'}:path.endsWith('/prepare')?{ready:true,message:'执行会话已准备；未下单。'}:path.endsWith('/check')?{ready:true}: {execution:{running:false,state:'idle'},lighter:{},bridge:{symbols:[]},feeds:{variational:{},lighter:{}},capture:{position:{known:false},chains:[],warnings:[],evidence:[]},diagnostics:[]}};},console
});
vm.runInContext(fs.readFileSync('src/web/static/js/operations.js','utf8').replace(/\}\)\(\);\s*$/,
 'globalThis.testVenueMinimum=estimateVenueMinimum;})();'),context);
(async()=>{
 // Arcus publishes a fractional minimum and step with unknown decimals.
 // Missing precision must never round 0.01 ZEC up to one whole ZEC.
 const minimum=context.testVenueMinimum({min_base_amount:.01,min_quote_amount:5,
  size_decimals:null,metadata:{step_size:'0.000001'}},1284.381);
 assert.equal(minimum.requiredBase,.01);
 assert.ok(Math.abs(minimum.effective-12.84381)<1e-10);
 assert.equal(context.testVenueMinimum({min_base_amount:.01,min_quote_amount:5,size_decimals:0},1284.381).requiredBase,1,
  'explicit zero decimals still requires integer quantity');
 assert.equal(context.testVenueMinimum({min_base_amount:null,min_quote_amount:null,size_decimals:null},1284).known,false);
 assert.equal(context.testVenueMinimum({min_base_amount:'',min_quote_amount:false,size_decimals:''},1284).known,false);
 assert.equal(context.testVenueMinimum({min_base_amount:0,min_quote_amount:5,size_decimals:null,metadata:{step_size:'.01'}},3).requiredBase,1.67,
  'metadata step also rounds minimum quote amounts to the actual lot grid');
 init();await new Promise(resolve=>setImmediate(resolve));
 assert.equal(node('live-arm').disabled,true);
 await node('execution-prepare').onclick();
 assert.equal(node('live-arm').disabled,true);
 assert.match(node('execution-arm-status').textContent,/下一步点击/);
 await node('execution-check').onclick();
 assert.equal(node('live-arm').disabled,false);
 assert.equal(node('execution-once').disabled,false);
 assert.match(node('execution-arm-status').textContent,/剩余 120 秒/);
 node('live-arm').onclick();
 assert.equal(requests.some(path=>path.endsWith('/monitor')),true);
 assert.equal(node('execution-once').disabled,true);
 await new Promise(resolve=>setImmediate(resolve));
 now+=120001;timers[0]();
 assert.equal(node('live-arm').disabled,true);
 assert.equal(node('execution-once').disabled,true);
 await node('execution-check').onclick();node('execution-cap').oninput();
 assert.equal(node('live-arm').disabled,true);
 assert.match(node('execution-arm-status').textContent,/金额已修改/);
 await node('execution-check').onclick();node('execution-temporary-cap').checked=true;
 node('execution-temporary-cap').onchange();
 assert.equal(node('live-arm').disabled,true);
 assert.match(node('execution-arm-status').textContent,/金额模式已修改/);
 for(const id of ['book-utilization','leverage','second-leg-retries','loss-limits','slippage','max-trades','execution-target-window','execution-funding-reserve','execution-close-target']){
  await node('execution-check').onclick();
  assert.equal(node('live-arm').disabled,false);
  node(id).onchange();
  assert.equal(node('live-arm').disabled,true,`${id} edits must invalidate the previous check`);
  assert.match(node('execution-strategy-summary').textContent,/有未保存修改/);
  events.get('strategy-config-saved')();
 }
 await node('execution-reset-stop').onclick();
 assert.equal(node('live-arm').disabled,true);
 assert.equal(node('execution-once').disabled,true);
 assert.match(node('execution-arm-status').textContent,/停止已解除/);
 assert.equal(requests.some(path=>path.endsWith('/once')),false);
 responseMode='blocked';await node('execution-check').onclick();
 assert.equal(node('live-arm').disabled,true);
 assert.match(node('execution-arm-status').textContent,/检查已完成但未通过/);
 assert.match(node('execution-check-result').textContent,/Lighter 费率证据过期/);
 responseMode='html';await node('execution-check').onclick();
 assert.equal(node('live-arm').disabled,true);
 assert.match(node('execution-arm-status').textContent,/检查请求失败/);
 assert.match(node('execution-check-result').textContent,/返回了 HTML 页面/);
 assert.match(node('execution-arm-status').textContent,/返回了 HTML 页面/,'the primary failure banner must include the actual cause');
 console.log('Execution controls: prepare, check, countdown, expiry and amount changes PASS; no orders');
})().catch(error=>{console.error(error);process.exitCode=1;});
