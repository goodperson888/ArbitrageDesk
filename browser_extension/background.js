chrome.runtime.onMessage.addListener((message, sender, respond) => {
    if (!['quote', 'manual_event'].includes(message.type) || !sender.url?.startsWith('https://omni.variational.io/')) return;
    (async () => {
        const {token} = await chrome.storage.local.get('token');
        if (!token) return;
        if (message.type === 'manual_event') {
            const event = message.event;
            if (!event || typeof event.url !== 'string' || !['GET', 'POST'].includes(event.method)) return;
            const r = await fetch('http://127.0.0.1:5001/api/bridge/manual-event', {
                method: 'POST', headers: {'Content-Type': 'application/json', Authorization: 'Bearer ' + token},
                body: JSON.stringify({url: event.url, method: event.method, status: event.status,
                    phase: event.phase, body: event.body, capture_id: event.capture_id})
            });
            await chrome.storage.local.set({lastStatus: r.ok ? '手动观测事件已记录 ' + new Date().toLocaleTimeString() : '面板拒绝观测事件 HTTP ' + r.status});
            return;
        }
        const q = message.quote;
        if (q?.source_url !== 'https://omni.variational.io/api/quotes/indicative') return;
        // Reconstruct the allowlist: never forward arbitrary page-provided properties.
        const safe = {};
        for (const key of ['source_url', 'symbol', 'instrument_type', 'qty', 'bid', 'ask', 'observed_at', 'timestamp', 'quote_id_present', 'page_estimated_slippage_bps', 'page_max_slippage_bps', 'page_side', 'estimated_fees_bid', 'estimated_fees_ask', 'qty_limits', 'page_funding_bps', 'page_funding_interval_s', 'page_funding_next_in_s']) safe[key] = q[key];
        try {
            const r = await fetch('http://127.0.0.1:5001/api/bridge/quote', {
                method: 'POST', headers: {'Content-Type': 'application/json', Authorization: 'Bearer ' + token},
                body: JSON.stringify(safe)
            });
            await chrome.storage.local.set({lastStatus: r.ok ? '报价已同步 ' + new Date().toLocaleTimeString() : '面板拒绝报价 HTTP ' + r.status});
        } catch { await chrome.storage.local.set({lastStatus: '本地面板未连接'}); }
    })().then(() => respond({ok: true})).catch(async () => {
        await chrome.storage.local.set({lastStatus: '本地面板未连接，采集未保存'});
        respond({ok:false});
    });
    return true;
});
