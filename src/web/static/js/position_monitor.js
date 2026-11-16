(() => {
    const names = {lighter:'Lighter', lighter_rh:'Lighter RH', arcus:'Arcus'};
    const age = stamp => {
        const value = Date.parse(stamp || '');
        return Number.isFinite(value) ? Math.max(0, (Date.now() - value) / 1000) : null;
    };
    const seconds = value => value == null ? '未收到' : value.toFixed(1) + ' 秒前';
    const amount = value => Number.isFinite(value) ? value.toFixed(5) + ' USDC' : '待核验';
    function exitSummary(row) {
        if (!row || row.heartbeat_stale || row.exit_profit_status === 'pending') return null;
        const estimateAge = age(row.exit_estimate_at);
        if (estimateAge != null && estimateAge > 2) return null;
        if (!Number.isFinite(row.exit_net_pnl_usdc)) return null;
        const state = row.convergence_ok === false ? '等待回归目标'
            : row.exit_profit_status === 'waiting'
                ? (row.convergence_ok === true ? '已回归 · ' : '')
                    + (Number.isFinite(row.exit_reference_net_pnl_usdc) && row.exit_reference_net_pnl_usdc <= 0
                        ? '盘口平仓仍未盈利' : '价格保护后净收益未达门槛')
                : '净盈利已达标，继续核验平仓条件';
        return {text:'保护价净收益 ' + amount(row.exit_net_pnl_usdc) + ' · ' + state,
            title:'当前盘口净收益（含费用及已结算资金费）：' + amount(row.exit_reference_net_pnl_usdc)
                + '；盘口冲击及价格保护扣减：' + amount(row.exit_movement_cost_usdc)
                + '；保护价净收益：' + amount(row.exit_net_pnl_usdc)
                + '；预计平仓手续费：' + amount(row.exit_closing_fee_usdc)
                + '（已核验账户费率 × 平仓金额，实扣待成交后核对）；'
                + (row.convergence_ok === true ? '已达到回归目标。' : row.convergence_ok === false ? '尚未达到本单回归目标。' : '')};
    }
    function rowText(row) {
        if (!row) return '持仓监控心跳待接入';
        const legs = Object.values(row.legs || {}).map(leg => {
            const connection = leg.connection || {};
            const mode = connection.blocked ? '长连接受阻' : connection.connected
                ? connection.quote_status === 'fresh' ? '长连接正常' : '长连接在线·报价待更新'
                : '长连接未连接';
            const source = leg.source && !leg.source.includes('websocket') ? ' · REST 回退' : '';
            return (names[leg.venue] || leg.venue) + ' ' + mode + source + ' · 报价 ' + seconds(age(leg.updated_at));
        });
        const quoteStale = Object.values(row.legs || {}).some(leg => age(leg.updated_at) == null || age(leg.updated_at) > 2);
        const status = row.heartbeat_status === 'stopped'
            ? '执行进程已停止' + (row.position_status === 'reconcile_required' ? '，持仓待核对' : '')
                + (row.monitor_stop_reason ? '：' + row.monitor_stop_reason : '')
            : row.heartbeat_stale ? '执行进程心跳超时，监控状态待核验' : row.position_status === 'reconcile_required'
            ? '持仓待核对，自动执行受阻' : row.phase === 'closing' || row.position_status === 'closing'
            ? '平仓执行中，等待成交核对' : row.error
            ? '平仓条件暂无法判断：' + row.error : row.account_status === 'pending'
            ? row.reason ? '已触发 ' + row.reason + '，等待账户核验' : '账户核验中' + (row.account_errors?.length ? '：' + row.account_errors.join('；') : '')
            : quoteStale ? '持仓报价已过期，等待新报价'
            : row.exit_profit_status === 'waiting' ? '等待净盈利达标 · ' + (exitSummary(row) ? exitSummary(row).text + '；' + exitSummary(row).title : '平仓测算已过期，等待新报价')
            : row.exit_profit_status === 'pending' ? '平仓净盈利测算中：' + (row.exit_profit_error || '等待盘口及账务核验')
            : row.convergence_ok === false ? '尚未达到本单回归目标'
            : row.last_success_at ? '平仓条件检查正常' : '等待首次检查';
        return [status, '最近成功检查 ' + seconds(age(row.last_success_at)), ...legs].join('；');
    }
    function summary(monitor, count = 0) {
        if (!monitor) return count ? '持仓监控：当前进程尚未提供心跳；新增净盈利平仓门槛需下次正常重启实盘后加载。' : '';
        if (!monitor.positions?.length) return '持仓监控：当前没有持仓。';
        const heartbeatAge = age(monitor.updated_at);
        const stale = !monitor.running || heartbeatAge == null || heartbeatAge > 3;
        return '持仓监控：' + monitor.positions.map(row => row.symbol + ' · ' + rowText({...row, heartbeat_stale:stale,
            heartbeat_status:monitor.heartbeat_status || (!monitor.running ? 'stopped' : stale ? 'stale' : 'live'),
            monitor_stop_reason:monitor.stop_reason})).join(' ｜ ')
            + (monitor.entry_paused ? '；账户待核验或监控异常，已暂停新开仓和加仓。' : '');
    }
    function brief(row) {
        if (!row) return '监控心跳待接入';
        if (row.heartbeat_status === 'stopped') return '执行已停止 · 持仓待核对';
        if (row.heartbeat_stale) return '监控心跳中断';
        if (row.phase === 'closing' || row.position_status === 'closing') return '平仓执行中';
        if (row.error) return '平仓判断受阻';
        if (row.account_status === 'pending') return '账户核验中';
        if (row.exit_profit_status === 'waiting') return '等待净盈利达标';
        if (row.exit_profit_status === 'pending') return '净盈利测算中';
        return row.last_success_at ? '已检查 · ' + seconds(age(row.last_success_at)) : '等待首次检查';
    }
    globalThis.positionMonitorRowText = rowText;
    globalThis.positionMonitorSummary = summary;
    globalThis.positionMonitorBrief = brief;
    globalThis.positionMonitorExitSummary = exitSummary;
    if (typeof document !== 'undefined' && typeof EventSource !== 'undefined') {
        let stream = null;
        const connect = () => {
            if (stream) return;
            stream = new EventSource('/api/position-monitor/stream');
            stream.onmessage = event => {
                try {
                    const data = JSON.parse(event.data);
                    const node = document.getElementById('execution-position-monitor');
                    if (!node) return;
                    globalThis.positionMonitorStreamActive = true;
                    globalThis.updateDeskMonitor?.(data);
                    node.textContent = data.error || summary(data.monitor, data.position_count);
                    node.className = 'chart-note ' + (data.error || data.monitor?.entry_paused || data.monitor?.heartbeat_stale ? 'warn' : '');
                } catch (_) { globalThis.positionMonitorStreamActive = false; }
            };
            stream.onerror = () => {
                globalThis.positionMonitorStreamActive = false;
                globalThis.updateDeskMonitor?.({error:'监控状态流重连中，请核对持仓监控'});
                const node = document.getElementById('execution-position-monitor');
                if (node) { node.textContent = '持仓监控状态流正在重连；请核对最近成功检查时间。'; node.className = 'chart-note warn'; }
            };
        };
        document.addEventListener('DOMContentLoaded', connect);
        window.addEventListener('pageshow', connect);
        window.addEventListener('pagehide', () => {
            if (stream) stream.close();
            stream = null;
            globalThis.positionMonitorStreamActive = false;
        });
    }
})();
