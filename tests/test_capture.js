const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const messages=[];
class XHR {open(){} send(){} }
let payload={bid:'100',ask:'101',quote_id:'private-quote-id',wallet:'secret',cookie:'secret',
 margin_requirements:{estimated_fees_bid:'0',estimated_fees_ask:'0',wallet:'secret'},
 qty_limits:{bid:{min_qty_tick:'.001',min_qty:'.001',max_qty:'10'},ask:{min_qty_tick:'.001',min_qty:'.001',max_qty:'10'}}};
const response={ok:true,status:200,clone(){return{json:async()=>payload}}};
const window={fetch:async()=>response,postMessage:m=>{if(!m.event?.body?.capture_error) messages.push(m)}};
const elements={
 'quantity-input':{value:'.01'}, 'input-mode-toggle':{textContent:'BTC '},
 'ask-price-display':{textContent:'$101',parentElement:{getAttribute:()=> 'true'}},
 'bid-price-display':{textContent:'$100',parentElement:{getAttribute:()=> 'false'}},
 'slippage-button':{textContent:'估: -0.03% / 最大: 0.1%'}
};
const document={body:{innerText:'8时\n资金费率 / 倒计时\n0.0092% / 07:30:27'},querySelector:s=>elements[s.match(/"([^"]+)"/)[1]]};
vm.runInNewContext(fs.readFileSync('browser_extension/capture.js','utf8'),{window,document,location:{origin:'https://omni.variational.io',pathname:'/perpetual/BTC'},URL,XMLHttpRequest:XHR,WeakMap,JSON,Date,crypto:require('node:crypto').webcrypto,TextEncoder});
const waitFor = async count => {
 for(let i=0;i<100 && messages.length<count;i++) await new Promise(r=>setTimeout(r,5));
 assert.equal(messages.length,count);
};
(async()=>{
 const body=JSON.stringify({instrument:{underlying:'BTC',instrument_type:'perpetual_future'},qty:'0.01',wallet:'secret'});
 const orderBody=JSON.stringify({quote_id:'private-quote-id',side:'buy',max_slippage:0.005,is_reduce_only:false,wallet:'secret'});
 await window.fetch('/api/quotes/indicative',{method:'POST',body});await waitFor(2);
 const quote=messages.find(x=>x.quote).quote;
 assert.equal(quote.qty,'0.01');
 assert.equal(quote.estimated_fees_bid,0);
 assert.equal(quote.qty_limits.ask.min_qty_tick,.001);
 assert.equal(quote.page_funding_interval_s,28800);
 assert.equal(quote.page_funding_bps,.92);
 assert.equal(quote.page_funding_next_in_s,27027);
 assert.equal(quote.page_estimated_slippage_bps,-3);
 assert.equal(quote.page_max_slippage_bps,10);
 assert.equal(quote.page_side,'buy');
 const fingerprint=messages.find(x=>x.event).event.body.quote_id_digest;
 payload={rfq_id:'private-rfq-id'};
 await window.fetch('/api/orders/new/market',{method:'POST',body:orderBody});await waitFor(4);
 const submit=messages.filter(x=>x.event?.url.endsWith('/api/orders/new/market')).map(x=>x.event);
 assert.equal(submit.length,2);
 const request=submit.find(x=>x.phase==='request'), responseEvent=submit.find(x=>x.phase==='response');
 assert.equal(request.body.quote_id_digest,fingerprint);
 assert.equal(request.capture_id,responseEvent.capture_id);
 assert.equal(responseEvent.body.rfq_id_present,true);
 payload=[{position_info:{instrument:{underlying:'ETH'},qty:'.00036',avg_entry_price:'2715',company:'private-company'},upnl:null}];
 await window.fetch('/api/positions');await waitFor(5);
 const position=messages.at(-1).event.body;
 assert.equal(position.rows[0].qty,.00036);
 assert.equal(position.rows[0].upnl,undefined);
 assert.equal(position.row_count,1);assert.equal(position.complete,true);
 payload=[{id:'private-transfer-id',rfq_id:'private-rfq',asset:'USDC',amount:'0.0001',fee_amount:'0.00001',
   funding_payment:'-0.00002',status:'settled',wallet_address:'private-wallet'}];
 await window.fetch('/api/transfers');await waitFor(6);
 const settlement=messages.at(-1).event.body;
 assert.equal(settlement.rows[0].fee_amount,.00001);
 assert.equal(settlement.rows[0].asset,'USDC');
 assert.equal(JSON.stringify(settlement).includes('private-'),false);
 assert.equal(JSON.stringify(messages).includes('secret'),false);
 assert.equal(JSON.stringify(messages).includes('private-'),false);
 await window.fetch('https://evil.example/api/quotes/indicative',{method:'POST',body:orderBody});
 assert.equal(await window.fetch('/api/public'),response);
 assert.equal(messages.length,6);
 payload={bid:'100',ask:'101',quote_id:'private-quote-id'};
 elements['quantity-input'].value='2';
 await window.fetch('/api/quotes/indicative',{method:'POST',body});await waitFor(8);
 const quotes=messages.filter(message=>message.quote);assert.equal(quotes[1].quote.page_side,undefined);
 // Request instances must be cloned for observation; preserve the original response.
 await window.fetch({url:'https://omni.variational.io/api/orders/new/market',method:'POST',clone:()=>({text:async()=>orderBody})});await waitFor(10);
 assert.equal(messages.filter(x=>x.event?.phase==='request').at(-1).event.body.quote_id_digest,fingerprint);
 await window.fetch(new URL('https://omni.variational.io/api/quotes/indicative'),{method:'POST',body});await waitFor(12);
 assert.equal(messages.filter(x=>x.quote).length,3);
 // Reproduce request-client cleanup aborting outstanding response clones.
 // Original response is returned only after its observation clone is consumed.
 let cleanedUp=false;
 response.clone=()=>({json:async()=>{
   await new Promise(r=>setTimeout(r,5));
   if(cleanedUp) {const e=new Error('aborted');e.name='AbortError';throw e;}
   return payload;
 }});
 const preserved=await window.fetch('/api/quotes/indicative',{method:'POST',body});
 cleanedUp=true;
 assert.equal(preserved,response);
 await waitFor(14);
 assert.equal(messages.filter(x=>x.quote).length,4);
 console.log('Capture tests passed: correlation, nested positions, no secrets, Request body, preserved response.');
})().catch(e=>{console.error(e);process.exit(1)});
