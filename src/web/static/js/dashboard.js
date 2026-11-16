/* Presentation only: reuse existing history and monitor deliveries, never fetch or submit. */
(() => {
    'use strict';
    const get = id => document.getElementById(id);
    const venues = {lighter:'Lighter', lighter_rh:'Lighter RH', arcus:'Arcus', standx:'StandX', variational:'Variational'};
    const positions = new Map();
    let monitor = null, streamError = '', historyLoaded = false;
    const text = (node, content) => { if (node && node.textContent !== content) node.textContent = content; };
    function renderPositions() {
        const link = get('desk-position-status');
        if (!link) return;
        const count = Math.max(positions.size, Number(monitor?.position_count || 0));
        let state = '', attention = false;
        if (count) {
            const rows = monitor?.positions || [];
            const heartbeatAge = Date.now() - Date.parse(monitor?.updated_at || '');
            if (streamError) { state = '状态重连中'; attention = true; }
            else if (!monitor) { state = '等待监控核验'; attention = true; }
            else if (!monitor.running || monitor.heartbeat_status === 'stopped') { state = '监控已停止'; attention = true; }
            else if (monitor.heartbeat_stale || !Number.isFinite(heartbeatAge) || heartbeatAge < 0 || heartbeatAge > 3000) {
                state = '心跳待核验'; attention = true;
            } else if (rows.some(row => row.position_status === 'reconcile_required' || row.error
                || (row.account_status && row.account_status !== 'ready'))) { state = '监控待核对'; attention = true; }
            else if (rows.length < count || rows.some(row => !row.last_success_at)) { state = '等待监控检查'; }
            else if (rows.some(row => row.phase === 'closing' || row.position_status === 'closing')) { state = '平仓中'; }
            else { state = '监控中'; }
        }
        text(link, count ? '持仓 ' + count + ' 组 · ' + state : historyLoaded ? '持仓 0 组' : '持仓读取中');
        link.classList.toggle('attention', attention);
        link.title = (streamError ? streamError + '；' : '') + '查看所有平台的未平仓订单；价差、盈亏和等待原因在订单详情中查看。';
    }
    globalThis.updateDeskPositions = data => {
        historyLoaded = true;
        // A history filter must never hide an unrelated live position.
        if (data.scope !== 'pair') positions.clear();
        for (const row of data.closed_positions || []) positions.delete(row.id);
        for (const row of data.open_positions || []) positions.set(row.id, true);
        renderPositions();
    };
    globalThis.updateDeskMonitor = data => {
        monitor = data.monitor ? {...data.monitor, position_count:data.position_count} : null;
        streamError = data.error || '';
        renderPositions();
    };
    globalThis.updateDeskExecution = execution => {
        get('desk-run')?.classList.toggle('is-running', Boolean(execution?.running));
        // Prefer the existing high frequency stream when present.
        if (!globalThis.positionMonitorStreamActive && execution?.position_monitor)
            globalThis.updateDeskMonitor({monitor:execution.position_monitor, position_count:execution.tracked_position_count});
        text(get('desk-scope-symbols'), execution?.running ? '本次运行品种：'+(execution.symbols || [execution.symbol]).filter(Boolean).join('、') : '');
        const scope = get('desk-active-scope');
        text(scope, execution?.running ? '本次运行：' + (execution.venues || []).map(v => venues[v] || v).join(' ↔ ')
            + ' · ' + (execution.symbols?.length || (execution.symbol ? 1 : 0)) + ' 个品种 · 改选用于下次启动'
            : '平台与品种在下方选择；只读检查通过后才能开启实盘。');
    };
    function groupSettings(parentId, ids, title) {
        const parent = get(parentId);
        if (!parent) return;
        const fields = ids.map(id => get(id)?.closest('label')).filter(Boolean);
        if (!fields.length) return;
        const details = document.createElement('details'); details.className = 'desk-disclosure desk-advanced-settings';
        const summary = document.createElement('summary'); summary.textContent = title;
        const grid = document.createElement('div'); grid.className = 'settings-grid';
        fields.forEach(field => grid.appendChild(field)); details.append(summary, grid); parent.appendChild(details);
    }
    document.addEventListener('DOMContentLoaded', () => {
        const toggle = (id, target, className) => get(id)?.addEventListener('change', event =>
            get(target)?.classList.toggle(className, event.target.checked));
        toggle('desk-opportunity-columns','cross-venue-scanner','desk-show-columns');
        get('desk-history-columns')?.addEventListener('change', event =>
            get('execution-history-panel')?.classList.toggle('desk-history-compact', !event.target.checked));
        document.querySelectorAll('.desk-nav a').forEach(link => link.addEventListener('click', () => {
            const target = get(link.getAttribute('href').slice(1));
            if (target?.tagName === 'DETAILS') target.open = true;
        }));
        get('desk-position-status')?.addEventListener('click', event => {
            event.preventDefault();
            globalThis.showOpenExecutionHistory?.();
        });
        groupSettings('entry-policy-settings', ['execution-funding-mode','execution-funding-reserve','execution-funding-hold','execution-funding-extra'], '进阶：资金费估算与预算');
        groupSettings('entry-policy-settings', ['execution-entry-quantile','execution-entry-min-samples'], '进阶：历史分位参数');
        groupSettings('position-policy-settings', ['leverage'], '进阶：杠杆');
    });
})();
