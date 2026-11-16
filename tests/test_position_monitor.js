'use strict';
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const context = vm.createContext({Date});
vm.runInContext(fs.readFileSync('src/web/static/js/position_monitor.js', 'utf8'), context);
const now = new Date().toISOString();
const row = {symbol:'SUI', last_success_at:now, account_status:'ready', legs:{
    first:{venue:'lighter', updated_at:now, source:'lighter_websocket',
           connection:{connected:true, quote_status:'fresh'}},
    second:{venue:'arcus', updated_at:now, source:'arcus_rest',
            connection:{connected:true, quote_status:'stale'}}}};
assert.match(context.positionMonitorRowText(row), /长连接正常/);
assert.match(context.positionMonitorRowText(row), /长连接在线·报价待更新 · REST 回退/);
assert.match(context.positionMonitorRowText({...row,error:'持仓报价过期'}), /平仓条件暂无法判断/);
assert.match(context.positionMonitorRowText({...row,heartbeat_stale:true}), /心跳超时/);
assert.match(context.positionMonitorRowText({...row,phase:'closing'}), /平仓执行中/);
assert.match(context.positionMonitorRowText({...row,exit_profit_status:'waiting',exit_net_pnl_usdc:-.02}), /等待净盈利达标.*-0.02000/);
assert.match(context.positionMonitorBrief({...row,exit_profit_status:'waiting'}), /等待净盈利达标/);
const profitRow={...row,exit_profit_status:'waiting',exit_net_pnl_usdc:-.01,
    exit_reference_net_pnl_usdc:.002,exit_movement_cost_usdc:.012,
    exit_closing_fee_usdc:.005,exit_estimate_at:now,convergence_ok:true};
assert.match(context.positionMonitorExitSummary(profitRow).text,/保护价净收益 -0.01000/);
assert.match(context.positionMonitorExitSummary(profitRow).title,/盘口净收益.*0.00200.*价格保护扣减.*0.01200/);
assert.match(context.positionMonitorRowText(profitRow),/已回归/);
assert.match(context.positionMonitorExitSummary(profitRow).text,/已回归 · 价格保护后净收益未达门槛/);
assert.match(context.positionMonitorExitSummary({...profitRow,exit_reference_net_pnl_usdc:-.002}).text,/已回归 · 盘口平仓仍未盈利/);
assert.match(context.positionMonitorExitSummary({...profitRow,convergence_ok:false}).text,/等待回归目标/);
assert.equal(context.positionMonitorExitSummary({...profitRow,heartbeat_stale:true}),null);
assert.equal(context.positionMonitorExitSummary({...profitRow,exit_estimate_at:'2000-01-01T00:00:00Z'}),null);
assert.match(context.positionMonitorRowText({...profitRow,exit_estimate_at:'2000-01-01T00:00:00Z'}),/测算已过期/);
assert.doesNotMatch(context.positionMonitorRowText({...profitRow,exit_estimate_at:'2000-01-01T00:00:00Z'}),/-0.01000/);
assert.match(context.positionMonitorRowText({...row,exit_profit_status:'pending',exit_profit_error:'资金费记录待补'}),/资金费记录待补/);
assert.match(context.positionMonitorRowText({...row,account_status:'pending',reason:'盈利条件'}), /已触发 盈利条件，等待账户核验/);
assert.match(context.positionMonitorSummary(null,1), /尚未提供心跳/);
assert.match(context.positionMonitorSummary({running:true,updated_at:'2000-01-01T00:00:00Z',positions:[row]}), /心跳超时/);
const stoppedRow={...row,heartbeat_stale:true,heartbeat_status:'stopped',
    position_status:'reconcile_required',monitor_stop_reason:'双腿账户数量不一致'};
assert.match(context.positionMonitorRowText(stoppedRow), /执行进程已停止，持仓待核对：双腿账户数量不一致/);
assert.doesNotMatch(context.positionMonitorRowText(stoppedRow), /心跳超时/);
assert.match(context.positionMonitorBrief(stoppedRow), /执行已停止/);
assert.match(context.positionMonitorSummary({running:false,updated_at:now,
    stop_reason:'缺少历史成交价或费用证据',positions:[{...row,position_status:'reconcile_required'}]}),
    /执行进程已停止，持仓待核对：缺少历史成交价或费用证据/);
assert.match(context.positionMonitorSummary({running:true,updated_at:now,positions:[row],entry_paused:true}), /暂停新开仓和加仓/);
const callbacks = {}, lifecycle = {}, node = {};
let stream;
const browserContext = vm.createContext({Date,
    document:{addEventListener:(name, fn) => {callbacks[name] = fn;}, getElementById:() => node},
    window:{addEventListener:(name, fn) => {lifecycle[name] = fn;}},
    EventSource:class {
        constructor(url) {this.url = url; stream = this;}
        close() {this.closed = true;}
    }});
vm.runInContext(fs.readFileSync('src/web/static/js/position_monitor.js', 'utf8'), browserContext);
callbacks.DOMContentLoaded();
assert.equal(stream.url, '/api/position-monitor/stream');
stream.onmessage({data:JSON.stringify({monitor:null,position_count:1})});
assert.equal(browserContext.positionMonitorStreamActive, true);
assert.match(node.textContent, /尚未提供心跳/);
stream.onerror();
assert.equal(browserContext.positionMonitorStreamActive, false);
assert.match(node.textContent, /正在重连/);
stream.onmessage({data:JSON.stringify({monitor:{running:true,updated_at:now,positions:[row]},position_count:1})});
assert.equal(browserContext.positionMonitorStreamActive, true);
assert.match(node.textContent, /SUI/);
lifecycle.pagehide();
assert.equal(stream.closed, true);
assert.equal(browserContext.positionMonitorStreamActive, false);
const oldStream = stream;
lifecycle.pageshow();
assert.notEqual(stream, oldStream);
assert.equal(stream.closed, undefined);
console.log('position monitor status tests passed');
