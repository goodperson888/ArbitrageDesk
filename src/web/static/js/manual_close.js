/* A user-confirmed group exit; the server only queues it for the existing worker. */
(() => {
    'use strict';
    const sending = new Set();
    const names = {lighter:'Lighter 主网', lighter_rh:'Lighter RH', arcus:'Arcus', standx:'StandX', variational:'Variational'};
    function feedback(message, failed = false) {
        const element = document.getElementById('history-manual-close-feedback');
        if (element) {
            element.textContent = message;
            element.hidden = false;
            element.dataset.failed = failed ? 'true' : 'false';
        }
    }
    function appendAction(position, cell) {
        const control = position.manual_close_control || {};
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'history-double-close';
        button.disabled = sending.has(position.id) || !control.available;
        button.textContent = sending.has(position.id) ? '提交中' : control.pending ? '双平待确认' : '双平';
        button.title = control.reason || '等待核验本组是否由当前执行进程接管';
        button.setAttribute('aria-label', position.symbol + ' 本组双平');
        button.addEventListener('keydown', event => event.stopPropagation());
        button.addEventListener('click', async event => {
            event.stopPropagation();
            if (button.disabled || sending.has(position.id)) return;
            const venues = [position.variational_venue, position.lighter_venue];
            const label = venues.map(v => names[v] || v).join(' / ');
            const message = '确认双平 ' + position.symbol + '（' + label + '）？\n'
                + '只平本组剩余数量：' + position.variational_size + ' / ' + position.lighter_size + '。\n'
                + '将忽略回归和盈利条件，可能亏损；两边只减仓并行发送，最终以成交回执为准。\n'
                + '全局新开仓状态保持不变。';
            if (!globalThis.confirm(message)) return;
            sending.add(position.id);
            button.disabled = true;
            button.textContent = '提交中';
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 10000);
            try {
                const response = await fetch('/api/execution/manual-close', {
                    method:'POST', cache:'no-store', signal:controller.signal,
                    headers:{'Content-Type':'application/json', 'X-Local-Token':document.querySelector('meta[name="local-token"]')?.content || ''},
                    body:JSON.stringify({position_id:position.id, symbol:position.symbol, venues})
                });
                const result = await response.json();
                if (!response.ok || result.success !== true) throw new Error(result.message || result.error || '双平请求未通过');
                button.textContent = '双平待确认';
                feedback(result.message || '双平已排队，请等待双边成交回执。');
            } catch (error) {
                feedback(error.name === 'AbortError' ? '双平请求响应超时，结果待核对；正在刷新本组状态，请勿重复操作。'
                    : '双平请求：' + (error.message || '结果待核对，请刷新记录'), true);
            } finally {
                clearTimeout(timer);
                sending.delete(position.id);
                // Refresh the server's acknowledgement rather than claiming a
                // queued request was already a pair of confirmed fills.
                if (typeof loadExecutionHistory === 'function') await loadExecutionHistory();
            }
        });
        cell.appendChild(button);
        return button;
    }
    globalThis.appendManualCloseAction = appendAction;
})();
