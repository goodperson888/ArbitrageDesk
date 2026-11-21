'use strict';
// Execute the actual dashboard with a small DOM/API stub: never write user config.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const nodes = new Map();
const node = id => {
    if (!nodes.has(id)) nodes.set(id, {value:'', textContent:'', appendChild(){}, replaceChildren(){}, append(){}});
    return nodes.get(id);
};
const config = {strategy:{spread_threshold:{min:5,max:50},trading:{position_size:100,max_positions:1,max_total_position_notional:0,leverage:5,second_leg_retries:1},risk:{max_loss_per_trade:50,max_daily_loss:200,slippage_tolerance:10,entry_extra_profit_bps:3},wash_trading:{max_trades_per_day:100},entry_policy:{mode:'fixed',quantile:90,min_samples:300},close_policy:{mode:'convergence',target_mode:'center',convergence_basis:'remaining',convergence_bps:2,min_profit_usdc:0,min_profit_bps:0,cost_reserve_usdc:0,max_hold_seconds:3600},cost_model:{variational_fee_bps:0,funding_bps_per_8h:1}}};
config.strategy.position_policy={mode:'single',step_bps:5,max_layers:3,layer_notional_usdc:12,total_notional_usdc:36};
Object.assign(config.strategy.trading,{capital_reserve_percent:17,capital_reserve_usdc:4.5,capital_allocation_mode:'remaining_slots'});
Object.assign(config.strategy.entry_policy,{calculation_version:2,target_window_seconds:604800,funding_reserve_bps:2});
let saved;
Object.assign(config.strategy.risk, {book_utilization_percent:80,execution_price_movement_bps:2,hedge_max_delay_seconds:5,
    route_rollback_limit:2,route_rollback_cooldown_seconds:900});
Object.assign(config.strategy.entry_policy, {funding_mode:'fixed',funding_hold_seconds:3600,funding_extra_bps:1});
const context = vm.createContext({
    window:{dispatchEvent(){}},
    CustomEvent:class {constructor(type,options){this.type=type;this.detail=options?.detail;}},
    document:{getElementById:node,addEventListener(){},createElement:()=>({})},
    fetch:async(path,options)=>({ok:true,json:async()=>{
        if(options?.body){saved=JSON.parse(options.body);return {};}
        return path==='/api/config'?(saved||config):{};
    }}),
    Date, console,
});
vm.runInContext(fs.readFileSync('src/web/static/js/app.js','utf8'), context);
vm.runInContext('connect=()=>{}', context);
(async()=>{
    assert.equal(vm.runInContext('percent(1)',context), '+0.0100');
    assert.equal(vm.runInContext('percent(-12.34)',context), '-0.1234');
    assert.equal(vm.runInContext('basisPoints(7)',context), '+7.00 bps');
    assert.equal(vm.runInContext('percent(null)',context), '--');
    assert.equal(vm.runInContext('toPercent(undefined)',context), null);
    vm.runInContext(`renderHedgeCost({hedge_cost:{funding_source:'website_display',funding_rate_bps:.92,funding_interval_s:28800,directions:{a:{funding_estimate_usdc:.00092},b:{funding_estimate_usdc:-.00092}}}})`,context);
    assert.match(node('funding-source-a').textContent,/官网预计 \+0\.0092%/);
    assert.match(node('funding-a').textContent,/预计支付.*8小时/);
    assert.match(node('funding-b').textContent,/预计收取.*8小时/);
    vm.runInContext('renderHedgeCost({})',context);
    assert.equal(node('funding-a').textContent,'未知');
    assert.match(node('funding-source-a').textContent,/未知/);
    await vm.runInContext('loadConfig()',context);
    assert.equal(node('min-spread').value, 5);
    assert.equal(node('max-spread').value, 50);
    assert.equal(node('execution-target-window').value, 604800);
    assert.equal(node('execution-funding-reserve').value, 2);
    assert.equal(node('slippage').value, 0.1);
    assert.equal(node('execution-price-movement').value, 2);
    assert.equal(node('book-utilization').value, 80);
    assert.equal(node('hedge-max-delay').value, 5);
    assert.equal(node('route-rollback-limit').value, 2);
    assert.equal(node('route-rollback-cooldown').value, 15);
    assert.equal(node('position-mode').value, 'single');
    assert.equal(node('grid-step').value, .05);
    assert.equal(node('close-mode').value, 'convergence');
    assert.equal(node('close-convergence-basis').value, 'remaining');
    assert.equal(node('close-convergence-bps').value, 2);
    assert.equal(node('close-cost-reserve-usdc').value, 0);
    assert.equal(node('close-max-hold-minutes').value, 60);
    await vm.runInContext('saveConfig()',context);
    assert.deepEqual(saved, config, 'loading and saving percentages must preserve every original threshold');
    node('book-utilization').value='65';
    await vm.runInContext('saveConfig()',context);
    assert.equal(saved.strategy.risk.book_utilization_percent,65);
    await vm.runInContext('loadConfig()',context);
    assert.equal(node('book-utilization').value,65);
    node('execution-funding-mode').value='dynamic';
    node('execution-funding-hold').value='90';
    node('execution-funding-extra').value='0.5';
    await vm.runInContext('saveConfig()',context);
    assert.equal(saved.strategy.entry_policy.funding_mode,'dynamic');
    assert.equal(saved.strategy.entry_policy.funding_hold_seconds,5400);
    assert.equal(saved.strategy.entry_policy.funding_extra_bps,0.5);
    assert.equal(saved.strategy.entry_policy.funding_reserve_bps,2);
    await vm.runInContext('loadConfig()',context);
    assert.equal(node('execution-funding-hold').value,90);
    assert.equal(node('execution-funding-extra').value,0.5);
    node('position-mode').value='spread_grid';node('grid-step').value='0.025';node('grid-max-layers').value='4';
    node('grid-layer-notional').value='10';node('grid-total-notional').value='40';node('max-positions').value='4';
    await vm.runInContext('saveConfig()',context);
    assert.deepEqual(saved.strategy.position_policy,{mode:'spread_grid',step_bps:2.5,max_layers:4,layer_notional_usdc:10,total_notional_usdc:40});
    node('position-mode').value='single';await vm.runInContext('saveConfig()',context);
    assert.equal(saved.strategy.position_policy.mode,'single');
    node('min-spread').value = '0.7';
    node('slippage').value = '0.125';
    await vm.runInContext('saveConfig()',context);
    assert.equal(saved.strategy.spread_threshold.min, 0.7);
    assert.equal(saved.strategy.risk.slippage_tolerance, 12.5);
    node('route-rollback-cooldown').value='20';
    await vm.runInContext('saveConfig()',context);
    assert.equal(saved.strategy.risk.route_rollback_cooldown_seconds,1200);
    node('loss-limits').value='off';
    await vm.runInContext('saveConfig()',context);
    assert.equal(saved.strategy.risk.max_loss_per_trade,null);
    assert.equal(saved.strategy.risk.max_daily_loss,null);
    node('history-range').value='300';node('chart-mode').value='reference';
    context.capture = options => { context.chartResult=options; };
    vm.runInContext(`chart={setOption:capture};rows=[{timestamp:new Date().toISOString(),buy_variational_sell_lighter_bps:12.34,reference_buy_variational_sell_lighter_bps:12.34,buy_lighter_sell_variational_bps:-10,reference_buy_lighter_sell_variational_bps:-10,mark_spread_bps:null}];renderChart()`, context);
    const lines = context.chartResult.series;
    assert.equal(lines[0].data[0].value[1], 0.1234);
    assert.equal(lines[1].data[0].value[1], -0.1);
    assert.equal(lines.length, 2, 'only available direction series are rendered');
    vm.runInContext(`rows=[{timestamp:Date.now()/1000,pair_key:'arcus->lighter',buy_venue:'arcus',sell_venue:'lighter',gross_bps:100,spread_basis:'buy_ask'},
        {timestamp:Date.now()/1000,pair_key:'lighter->arcus',buy_venue:'lighter',sell_venue:'arcus',gross_bps:-120,spread_basis:'buy_ask'}];renderChart()`,context);
    assert.equal(context.chartResult.series.length,1,'new multi-venue history has one canonical axis per pair');
    assert(Math.abs(context.chartResult.series[0].data[0].value[1]-100/1.01/100)<1e-10,
        'history ratio observations are normalized to the execution denominator exactly once');
    console.log('Dashboard percent formatting, configuration round trip, edited thresholds and chart gaps: PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
