(function () {
    'use strict';
    const API = 'http://127.0.0.1:5002/api/v2';
    const $ = id => document.getElementById(id);
    const PREF_KEY = 'arbdesk.crossVenuePrefs';
    let savedPrefs = {};
    try { savedPrefs = JSON.parse(localStorage.getItem(PREF_KEY) || '{}') || {}; } catch (_) { savedPrefs = {}; }
    const validRefreshMs = value => [2000, 5000, 10000, 30000].includes(Number(value)) ? Number(value) : 0;
    const state = {
        venues: [], instruments: {}, venueDiagnostics: {}, venueRecovery: {}, venueProbesPending: 0, timer: null, loading: false, scanController: null,
        auto: true, mode: 'all', symbolMode: savedPrefs.symbolMode === 'selected' ? 'selected' : 'all',
        catalogStatus: {}, catalogRequests: new Map(), catalogTimer: null,
        rows: [], allRows: [], historyStats: new Map(), reversionStats: new Map(),
        reversionLoading: false, reversionRequestId: 0, reversionAttempts: new Map(),
        spreadMinBps: Number(savedPrefs.spreadMinBps) >= 0 ? Number(savedPrefs.spreadMinBps) : 0,
        spreadMaxBps: Number(savedPrefs.spreadMaxBps) > 0 ? Number(savedPrefs.spreadMaxBps) : 0,
        lastScanFailures: [],
        selectedVenues: Array.isArray(savedPrefs.selectedVenues) ? savedPrefs.selectedVenues.filter(value => typeof value === 'string') : ['lighter', 'arcus'],
        selectedSymbols: Array.isArray(savedPrefs.selectedSymbols) ? savedPrefs.selectedSymbols.filter(value => typeof value === 'string') : [],
        excludedSymbols: Array.isArray(savedPrefs.excludedSymbols) ? savedPrefs.excludedSymbols.filter(value => typeof value === 'string') : [],
        liveSocket: null, liveConnected: false, liveSymbolsKey: '', liveSymbols: [],
        liveStreamWanted: false, liveReconnectTimer: null, liveReconnectAttempt: 0,
        liveStartedAt: null, liveLastMessageAt: null,
        scanStartedAt: null, hiddenStaleCount: 0
    };
    state.refreshMs = validRefreshMs(savedPrefs.refreshMs);
    state.historyWindow = Number(savedPrefs.historyWindow) === 604800 ? 604800 : 86400;
    state.reversionWindow = [86400, 259200, 604800].includes(Number(savedPrefs.reversionWindow)) ? Number(savedPrefs.reversionWindow) : 604800;
    state.reversionMinCount = Number(savedPrefs.reversionMinCount) >= 0 ? Number(savedPrefs.reversionMinCount) : 0;
    state.reversionMinDeviationBps = Number(savedPrefs.reversionMinDeviationBps) >= 0 ? Number(savedPrefs.reversionMinDeviationBps) : 0;
    state.reversionRecencyMode = savedPrefs.reversionRecencyMode === 'soft' ? 'soft' : 'hard';
    state.reversionMaxAge = Number(savedPrefs.reversionMaxAge) >= 0 ? Number(savedPrefs.reversionMaxAge) : 0;
    state.reversionMinSamples = Number(savedPrefs.reversionMinSamples) >= 0 ? Number(savedPrefs.reversionMinSamples) : 0;
    state.reversionTargetMode = ['center', 'zero', 'either'].includes(savedPrefs.reversionTargetMode) ? savedPrefs.reversionTargetMode : 'center';
    state.lastResponseAt = null;
    state.lastBatchMs = null;
    let drawerRequestId = 0, drawerLiveTimer = null, drawerHistoryUpdate = null, drawerQuoteUpdate = null;
    const freshness = window.QuoteFreshness;
    function savePrefs() {
        try {
            localStorage.setItem(PREF_KEY, JSON.stringify({
                selectedVenues: state.selectedVenues,
                selectedSymbols: state.selectedSymbols,
                excludedSymbols: state.excludedSymbols,
                symbolMode: state.symbolMode,
                refreshMs: state.refreshMs,
                historyWindow: state.historyWindow,
                spreadMinBps: state.spreadMinBps,
                spreadMaxBps: state.spreadMaxBps,
                reversionWindow: state.reversionWindow,
                reversionMinCount: state.reversionMinCount,
                reversionMinDeviationBps: state.reversionMinDeviationBps,
                reversionMaxAge: state.reversionMaxAge,
                reversionRecencyMode: state.reversionRecencyMode,
                reversionMinSamples: state.reversionMinSamples,
                reversionTargetMode: state.reversionTargetMode
            }));
        } catch (_) { /* private browsing can disable localStorage */ }
    }
    const labels = { variational: 'Variational', lighter: 'Lighter 主网', lighter_rh: 'Lighter RH · Robinhood Chain', standx: 'StandX', binance: 'Binance Futures', gate: 'Gate Futures', okx: 'OKX Swap', arcus: 'Arcus' };
    const names = value => labels[value] || value;
    window.getSelectedVenues = () => state.selectedVenues.slice();
    // Settings describe the user's authorization scope. Live candidates can
    // reorder or disappear on every quote without changing that scope.
    window.getExecutionScope = () => ({
        mode: state.symbolMode === 'all' ? 'all' : 'selected',
        symbols: state.symbolMode === 'all' ? [] : state.selectedSymbols.slice(),
        excluded_symbols: state.excludedSymbols.slice()
    });
    // The execution panel uses the same comparison scope as the scanner. The
    // scanner's top-level all/selected choice is the single source of truth;
    // execution must not maintain a second symbol-range selector.
    window.getExecutionSelection = () => {
        const all = state.symbolMode === 'all';
        // Freeze the authorized catalog, never the currently visible or
        // filtered candidates. The worker re-evaluates history and quotes
        // for every symbol, including ones that qualify only later.
        const symbols = all ? commonSymbols(state.selectedVenues) : state.selectedSymbols.slice();
        const excluded = new Set(state.excludedSymbols.map(symbolBase));
        return { mode: all ? 'all' : 'selected',
            symbols: [...new Set(symbols.map(symbolBase))].filter(symbol => symbol && !excluded.has(symbol)),
            gross_spread_min_bps: state.spreadMinBps, gross_spread_max_bps: state.spreadMaxBps,
            reversion_target_mode: state.reversionTargetMode,
            reversion_window_seconds: state.reversionWindow,
            reversion_min_count: state.reversionMinCount,
            reversion_min_deviation_bps: state.reversionMinDeviationBps,
            reversion_max_age_seconds: state.reversionMaxAge,
            reversion_recency_mode: state.reversionRecencyMode,
            reversion_min_samples: state.reversionMinSamples };
    };
    const capabilityLabel = venue => {
        if (venue.venue === 'arcus') return 'BBO / L2 盘口 · 实盘需授权';
        const capabilities = venue.capabilities || {};
        if (capabilities.order_submission) return '支持实盘';
        if (capabilities.executable_quotes) return '可执行报价';
        return '行情可用';
    };

    async function apiFetch(path, timeout = 9000, signal = undefined, withCoverage = false) {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), timeout);
        const abort = () => controller.abort();
        signal?.addEventListener('abort', abort, { once: true });
        if (signal?.aborted) controller.abort();
        try {
            const response = await fetch(API + path, { cache: 'no-store', signal: controller.signal });
            if (!response.ok) {
                const body = await response.json().catch(() => null);
                throw new Error(typeof body?.detail === 'string' ? body.detail : 'HTTP ' + response.status);
            }
            const rows = await response.json();
            return withCoverage ? { rows, coverage: response.headers.get('X-Scan-Coverage') } : rows;
        } finally { clearTimeout(timer); signal?.removeEventListener('abort', abort); }
    }
    function setApiState(label, tone) {
        const node = $('v2-api-state');
        if (!node) return;
        node.textContent = label;
        node.className = 'pill ' + (tone || 'neutral');
    }
    function renderVenueState() {
        const root = $('v2-venue-status');
        if (!root) return;
        root.replaceChildren();
        state.venues.forEach(venue => {
            const card = document.createElement('div');
            card.className = 'v2-status-card';
            card.dataset.venue = venue.venue;
            const title = document.createElement('strong'); title.textContent = names(venue.venue);
            const status = document.createElement('span');
            status.className = 'v2-status-value ' + (venue.status === 'degraded' ? 'warn' : 'ok');
            const tradeState = capabilityLabel(venue);
        status.textContent = venue.status === 'degraded' ? '连接异常' : (venue.venue === 'arcus' ? 'BBO 可用 · 实盘待授权' : '行情可用');
            const detail = document.createElement('small');
            detail.textContent = tradeState + ' · ' + (venue.capabilities?.catalog_mode === 'full' ? '完整目录' : '用户指定目录') + ' · ' + (venue.capabilities?.market_data || []).join(' / ');
            card.append(title, status, detail);
            if (venue.last_error) { const error = document.createElement('small'); error.className = 'v2-error'; error.textContent = venue.last_error; card.appendChild(error); }
            root.appendChild(card);
            const rail = document.querySelector('[data-v2-venue="' + venue.venue + '"] .venue-state');
            if (rail) { rail.className = 'venue-state' + (venue.status === 'degraded' ? ' planned' : ''); rail.lastChild.textContent = venue.status === 'degraded' ? '连接异常' : tradeState; }
        });
    }
    function venueHealth(value, now = Date.now()) {
        const venue = state.venues.find(item => item.venue === value) || {};
        const diagnostic = state.venueDiagnostics[value];
        const fallback = venue.capabilities?.catalog_mode === 'full' ? '完整目录；尚未读取选中品种报价' : '仅支持配置的指定品种';
        const catalog = state.catalogStatus[value];
        if (catalog?.error && !(state.instruments[value] || []).length)
            return {tone:'waiting', detail:catalog.error + '；目录自动重试中，暂不参与本页行情扫描'};
        if (diagnostic?.ok === true) {
            if (Number.isFinite(diagnostic.quoteAt) && now - diagnostic.quoteAt > 10000)
                return {tone:'waiting', detail:'上次行情已过期；等待新鲜报价自动恢复（平台颜色仅表示只读行情状态）'};
            return {tone:'ok', detail:diagnostic.detail};
        }
        const detail = diagnostic?.detail || venue.last_error || fallback;
        if (/REST 共享节流|参考价探测冷却|HTTP 429/.test(detail)) {
            const remaining = Number.isFinite(diagnostic?.retryUntil) ? Math.max(0, Math.ceil((diagnostic.retryUntil-now)/1000)) : null;
            return {tone:'waiting', detail:remaining === 0
                ? 'REST 探测等待已结束；等待新鲜报价自动恢复；不会阻断已到达的 WebSocket 行情'
                : detail+'；只读 REST 暂缓，收到新鲜行情后自动恢复'};
        }
        if (diagnostic && Number.isFinite(diagnostic.checkedAt) && now-diagnostic.checkedAt > 30000)
            return {tone:'waiting', detail:'上次探测未通过，等待当前报价确认；'+detail};
        const failed = diagnostic ? diagnostic.ok === false : venue.status === 'degraded' && Boolean(venue.last_error);
        return {tone:failed?'error':'ok', detail};
    }
    function updateVenuePickerHealth() {
        document.querySelectorAll('#v2-venue-select .venue-option').forEach(label => {
            const input=label.querySelector('input');
            if (!input) return;
            const health=venueHealth(input.value);
            label.classList.toggle('is-error',health.tone==='error');
            label.classList.toggle('is-waiting',health.tone==='waiting');
            label.title=health.detail;
            label.setAttribute('aria-label',names(input.value)+'：'+health.detail);
        });
    }
    function updateVenueDiagnosticsFromRows(rows, received = Date.now()) {
        // Use the already received, unfiltered quotes. No extra probes, and
        // no heartbeat/old cached row can manufacture a fresh venue state.
        for (const row of rows || []) {
            for (const side of ['buy','sell']) {
                const venue=row[side+'_venue'], price=row[side+'_price'], age=row[side+'_age_seconds'];
                if (!state.selectedVenues.includes(venue) || row[side+'_stale'] !== false ||
                    typeof price !== 'number' || !Number.isFinite(price) || price<=0 ||
                    typeof age !== 'number' || !Number.isFinite(age) || age<0) continue;
                const elapsed=Number.isFinite(row._receivedAt)?Math.max(0,(received-row._receivedAt)/1000):0;
                const seconds=age+elapsed+(row._deliverySeconds||0);
                if (seconds>10) continue;
                const quoteAt=received-seconds*1000;
                const old=state.venueDiagnostics[venue];
                if (old?.ok && Number.isFinite(old.quoteAt) && old.quoteAt>=quoteAt) continue;
                delete state.venueRecovery[venue];
                state.venueDiagnostics[venue]={ok:true,quoteAt,checkedAt:received,
                    detail:symbolBase(row.canonical_symbol)+' 已收到新鲜行情 · '+(row[side+'_source']||'来源未知')+'；仅表示只读行情恢复，下单仍需独立核验'};
            }
        }
        updateVenuePickerHealth();
    }
    function fillVenuePicker(node, values, formatter, selected) {
        if (!node) return;
        node.replaceChildren();
        values.forEach(value => {
            const health = venueHealth(value);
            const label = document.createElement('label'); label.className = 'venue-option' + (health.tone==='error'?' is-error':health.tone==='waiting'?' is-waiting':'');
            const input = document.createElement('input'); input.type = 'checkbox'; input.value = value; input.checked = selected.includes(value); input.setAttribute('aria-label', formatter(value));
            const text = document.createElement('span'); text.textContent = formatter(value);
            const detail = health.detail;
            label.title = detail;
            label.setAttribute('aria-label', formatter(value) + '：' + detail);
            label.append(input, text); node.appendChild(label);
        });
    }
    function selectedVenues() {
        const node = $('v2-venue-select');
        return node ? [...node.querySelectorAll('input[type="checkbox"]:checked')].map(input => input.value) : state.selectedVenues;
    }
    function symbolBase(value) { return (value || '').replace(/-PERP$/, ''); }
    function selectedSymbols() {
        const node = $('v2-symbol-select');
        return node ? [...node.querySelectorAll('input[type="checkbox"]:checked')].map(input => input.value) : state.selectedSymbols;
    }
    function catalogSymbols() {
        const result = new Set();
        Object.values(state.instruments).forEach(rows => rows.forEach(row => { if (row.enabled !== false) result.add(row.canonical_symbol); }));
        return [...result].sort();
    }
    function renderSymbolCatalog() {
        const root = $('v2-symbol-select');
        const mode = $('v2-symbol-mode');
        const symbols = catalogSymbols().map(symbolBase);
        if (!root) return;
        const current = $('market-select')?.value;
        const excluded = new Set(state.excludedSymbols);
        state.selectedSymbols = state.selectedSymbols.filter(symbol => symbols.includes(symbol) && !excluded.has(symbol));
        if (!state.selectedSymbols.length) state.selectedSymbols = current && symbols.includes(current) && !excluded.has(current) ? [current] : symbols.filter(symbol => !excluded.has(symbol)).slice(0, 1);
        root.replaceChildren();
        symbols.forEach(symbol => {
            const label = document.createElement('label'); label.className = 'symbol-option';
            const isExcluded = excluded.has(symbol);
            if (isExcluded) label.classList.add('is-excluded');
            const input = document.createElement('input'); input.type = 'checkbox'; input.value = symbol; input.checked = state.selectedSymbols.includes(symbol); input.disabled = isExcluded; input.setAttribute('aria-label', symbol + (isExcluded ? '，已排除' : ''));
            const text = document.createElement('span'); text.textContent = isExcluded ? symbol + ' · 已排除' : symbol;
            label.append(input, text); root.appendChild(label);
        });
        root.closest('.symbol-picker')?.classList.toggle('is-all-mode', state.symbolMode === 'all');
        if (mode) mode.value = state.symbolMode;
        const hint = $('v2-symbol-hint');
        if (hint) {
            const commonCount = state.selectedVenues.length >= 2 ? commonSymbols(state.selectedVenues).length : 0;
            hint.textContent = state.symbolMode === 'all'
                ? `可配对目录 · ${commonCount || 0} 个品种（任意两家共同支持）`
                : `已选 ${state.selectedSymbols.length} 个品种`;
        }
        filterSymbolOptions();
    }
    function renderExcludedCatalog() {
        const root = $('v2-exclude-select');
        if (!root) return;
        const symbols = catalogSymbols().map(symbolBase);
        const excluded = new Set(state.excludedSymbols);
        root.replaceChildren();
        symbols.forEach(symbol => {
            const label = document.createElement('label'); label.className = 'symbol-option exclude-option';
            const input = document.createElement('input'); input.type = 'checkbox'; input.value = symbol; input.checked = excluded.has(symbol); input.setAttribute('aria-label', '排除 ' + symbol);
            const text = document.createElement('span'); text.textContent = symbol;
            label.append(input, text); root.appendChild(label);
        });
        const hint = $('v2-exclude-hint');
        if (hint) hint.textContent = state.excludedSymbols.length ? '已排除 ' + state.excludedSymbols.length + ' 个' : '未设置';
        filterExcludedOptions();
    }
    function filterSymbolOptions() {
        const query = ($('v2-symbol-search')?.value || '').trim().toUpperCase();
        document.querySelectorAll('#v2-symbol-select .symbol-option').forEach(option => {
            option.hidden = Boolean(query) && !option.textContent.toUpperCase().includes(query);
        });
    }
    function filterExcludedOptions() {
        const query = ($('v2-exclude-search')?.value || '').trim().toUpperCase();
        document.querySelectorAll('#v2-exclude-select .symbol-option').forEach(option => {
            option.hidden = Boolean(query) && !option.textContent.toUpperCase().includes(query);
        });
    }
    function updateHistoryVisibility() {
        const panel = $('history-details');
        if (!panel) return;
        const singleSymbol = state.symbolMode === 'selected' && state.selectedSymbols.length === 1;
        panel.hidden = !singleSymbol;
        if (!singleSymbol) panel.open = false;
    }
    function renderCatalog() {
        const venueNode = $('v2-venue-select');
        const venues = state.venues.map(item => item.venue);
        // Start with no platform selected. Scanning only begins after the
        // user explicitly chooses the comparison set.
        const previous = state.selectedVenues.filter(value => venues.includes(value));
        fillVenuePicker(venueNode, venues, value => names(value), previous);
        state.selectedVenues = selectedVenues();
        window.setHistoryVenues?.(state.selectedVenues);
        renderSymbolCatalog();
        renderExcludedCatalog();
        updateHistoryVisibility();
        if (venueNode) venueNode.closest('fieldset')?.toggleAttribute('disabled', venueNode.children.length === 0);
        document.querySelectorAll('.v2-status-card[data-venue]').forEach(card => card.classList.toggle('is-unselected', !state.selectedVenues.includes(card.dataset.venue)));
        const hint = $('v2-venue-hint');
        if (hint) hint.textContent = state.selectedVenues.length < 2
            ? '至少选择两个；当前已选 ' + state.selectedVenues.length + '/' + venues.length
            : '已选 ' + state.selectedVenues.length + '/' + venues.length + '；方向由系统自动比较';
        const summary = $('v2-catalog-summary');
        summary.textContent = state.selectedVenues.length
            ? '已选 ' + state.selectedVenues.length + '/' + venues.length + ' 个平台；方向由系统自动比较'
            : '未选择平台；选择至少两个平台后开始读取行情。';
        const warning = catalogWarning(state.selectedVenues);
        if (warning) summary.textContent += '；目录未完整：' + warning;
    }
    function catalogWarning(venues) {
        return venues.filter(venue => !(state.instruments[venue] || []).length).map(venue => {
            const status = state.catalogStatus[venue];
            return names(venue) + '：' + (status?.error || '目录读取中') +
                (status?.retryAt ? '，自动重试中' : '');
        }).join('；');
    }
    function catalogVenues() {
        return state.selectedVenues.filter(venue => (state.instruments[venue] || []).length > 0);
    }
    function scheduleCatalogRetry() {
        clearTimeout(state.catalogTimer);
        state.catalogTimer = null;
        const targets = state.selectedVenues.length ? state.selectedVenues : state.venues.map(item => item.venue);
        const retries = targets.filter(venue => !(state.instruments[venue] || []).length)
            .map(venue => state.catalogStatus[venue]?.retryAt).filter(Number.isFinite);
        if (!retries.length) return;
        state.catalogTimer = setTimeout(async () => {
            state.catalogTimer = null;
            const ready = await refreshCatalog();
            if (ready && state.auto && state.selectedVenues.length >= 2 && !state.loading) scan(false);
        }, Math.max(0, Math.min(...retries) - Date.now()));
    }
    async function refreshCatalog(force = false) {
        const targets = state.selectedVenues.length
            ? state.selectedVenues.slice()
            : state.venues.map(item => item.venue);
        if (!targets.length) return false;
        await Promise.all(targets.map(venue => {
            if (state.catalogRequests.has(venue)) return state.catalogRequests.get(venue);
            if ((state.instruments[venue] || []).length) return;
            if (!force && state.catalogStatus[venue]?.retryAt > Date.now()) return;
            const request = (async () => {
                try {
                    const rows = await apiFetch('/instruments?venue=' + encodeURIComponent(venue), 20000);
                    if (!Array.isArray(rows)) throw new Error('品种目录返回格式异常');
                    if (!rows.length) throw new Error('平台当前未返回可用永续品种');
                    state.instruments[venue] = rows;
                    state.catalogStatus[venue] = {loaded: true};
                } catch (error) {
                    const attempts = (state.catalogStatus[venue]?.attempts || 0) + 1;
                    state.catalogStatus[venue] = {attempts,
                        error: error.name === 'AbortError' ? '目录请求超时' : error.message,
                        retryAt: Date.now() + Math.min(60000, 10000 * 2 ** Math.min(attempts - 1, 3))};
                } finally { state.catalogRequests.delete(venue); }
            })();
            state.catalogRequests.set(venue, request);
            return request;
        }));
        renderCatalog();
        scheduleCatalogRetry();
        return commonSymbols(targets).length > 0;
    }
    function venueProbeSymbol(venue) {
        const available = (state.instruments[venue] || []).filter(row => row.enabled !== false)
            .map(row => symbolBase(row.canonical_symbol || row.venue_symbol)).filter(Boolean);
        return [...state.selectedSymbols, 'BTC', ...state.liveSymbols].map(symbolBase)
            .find(symbol => available.includes(symbol)) || available[0] || 'BTC';
    }
    function recoverVenueHealth(now = Date.now()) {
        // Reuse the existing one-second UI tick. Healthy streams add no
        // requests; at most one failed/stale venue is probed at a time.
        if ((!state.auto && !state.liveStreamWanted) || state.venueProbesPending) return;
        const venue = state.selectedVenues.find(value => {
            if (!(state.instruments[value] || []).length) return false;
            const diagnostic = state.venueDiagnostics[value];
            const lastEvidence = diagnostic?.ok ? diagnostic.quoteAt : diagnostic?.checkedAt;
            if (!Number.isFinite(lastEvidence) || now-lastEvidence < 30000) return false;
            return now >= Math.max(state.venueRecovery[value]?.retryAt || 0, diagnostic?.retryUntil || 0);
        });
        if (venue) void probeSelectedVenues([venue], true);
    }
    async function probeSelectedVenues(values, automatic = false) {
        const probeId=state.venueProbeId=(state.venueProbeId||0)+1;
        const selected = [...new Set(values || [])];
        if (!selected.length) { state.venueDiagnostics = {}; renderCatalog(); return; }
        state.venueProbesPending += 1;
        const result = (ok, detail, quote) => {
            const checkedAt=Date.now(), observed=Date.parse(quote?.fetched_at||'');
            const quoteAt=Math.min(checkedAt-(Number(quote?.age_seconds)||0)*1000,
                Number.isFinite(observed)?observed:checkedAt);
            if (ok && checkedAt-quoteAt>10000) { ok=false; detail='报价时间已过期；等待新鲜行情'; }
            const wait=detail.match(/(?:等待|暂停 REST 请求)\s*(\d+)\s*秒/);
            return {ok,detail,checkedAt,
                quoteAt:ok?quoteAt:null,
                retryUntil:wait?checkedAt+Number(wait[1])*1000:null};
        };
        const checks = await Promise.all(selected.map(async venue => {
            const symbol = venueProbeSymbol(venue);
            let detail = symbol+' 报价为空或已过期';
            for (let attempt = 0; attempt < (automatic ? 1 : 3); attempt++) {
                try {
                    const rows = await apiFetch('/quotes?symbol=' + encodeURIComponent(symbol) + '&venue=' + encodeURIComponent(venue), 20000);
                    const quote = rows[0] || {};
                    const ok = typeof quote.bid === 'number' && Number.isFinite(quote.bid) && quote.bid > 0 &&
                        typeof quote.ask === 'number' && Number.isFinite(quote.ask) && quote.ask > 0 && quote.stale === false &&
                        Number.isFinite(quote.age_seconds) && quote.age_seconds >= 0 && quote.age_seconds <= 10;
                    detail = ok ? symbol+' 报价正常 · ' + (quote.source || 'REST') : (quote.reason || detail);
                    if (ok || automatic || attempt === 2 || !/HTTP 405|超时/i.test(detail))
                        return [venue, result(ok, detail, quote)];
                } catch (error) {
                    detail = error.name === 'AbortError' ? symbol+' 报价请求超时' : symbol+' 报价请求失败：' + error.message;
                    if (automatic || attempt === 2 || !/HTTP 405|超时/i.test(detail))
                        return [venue, result(false, detail)];
                }
                await new Promise(resolve => setTimeout(resolve, 500 * (attempt + 1)));
            }
            return [venue, result(false, detail)];
        }));
        state.venueProbesPending -= 1;
        if (state.venueProbeId!==probeId) return;
        checks.forEach(([venue, diagnostic]) => {
            if (!state.selectedVenues.includes(venue)) return;
            const old=state.venueDiagnostics[venue];
            if (old?.ok && Number.isFinite(old.quoteAt) && Date.now()-old.quoteAt<=10000 &&
                (!diagnostic.ok || old.quoteAt>=diagnostic.quoteAt)) return;
            state.venueDiagnostics[venue]=diagnostic;
            if (diagnostic.ok) delete state.venueRecovery[venue];
            else {
                const attempts = (state.venueRecovery[venue]?.attempts || 0) + 1;
                state.venueRecovery[venue] = {attempts, retryAt: Math.max(diagnostic.retryUntil || 0,
                    Date.now() + Math.min(60000, 15000 * 2 ** Math.min(attempts - 1, 2)))};
            }
        });
        updateVenuePickerHealth();
    }
    function age(seconds) { return seconds == null ? '年龄未知' : (Number(seconds) < 1 ? '<1 秒' : Number(seconds).toFixed(1) + ' 秒'); }
    function bpsPercent(value) { return value == null ? '未知' : (Number(value) / 100).toFixed(4) + '%'; }
    function historyFields() {
        return state.historyWindow === 604800
            ? ['history_7d_samples', 'history_7d_min_bps', 'history_7d_max_bps', 'history_7d_avg_bps', '近7天']
            : ['history_samples', 'history_min_bps', 'history_max_bps', 'history_avg_bps', '近24小时'];
    }
    function updateHistoryWindowLabel() {
        const label = state.historyWindow === 604800 ? '历史价差（7天）' : '历史价差（24h）';
        const header = $('v2-history-header');
        if (header) header.textContent = label;
    }
    function historyRange(row) {
        const [samplesKey, minKey, maxKey] = historyFields();
        const samples = Number(row[samplesKey] || 0);
        if (!samples || row[minKey] == null || row[maxKey] == null) return '样本不足';
        return bpsPercent(row[minKey]) + ' ~ ' + bpsPercent(row[maxKey]);
    }
    function historyRangeDetail(row) {
        const [samplesKey, , , avgKey, label] = historyFields();
        const samples = Number(row[samplesKey] || 0);
        const rawSamples = Number(state.historyWindow === 604800 ? row.history_7d_raw_samples : row.history_raw_samples) || 0;
        const anomalies = Number(state.historyWindow === 604800 ? row.history_7d_anomaly_count : row.history_anomaly_count) || 0;
        if (!samples) return rawSamples ? label + '采集到 ' + rawSamples + ' 个样本，但均被标为异常，暂不用于回归统计' : label + '暂无已采集的该路线价差样本';
        const anomalyNote = anomalies ? '；异常点 ' + anomalies + ' 个已从中心、均值和极值中排除' : '';
        return label + '已采集 ' + samples + ' 个正常样本；平均 ' + bpsPercent(row[avgKey]) + anomalyNote + '。图表仍保留原始异常点，极值不代表可成交价格或未来一定回归。';
    }
    function spreadFilterAllowed(row) {
        const value = Math.abs(executionGross(row) ?? Number(row?.gross_bps));
        return Number.isFinite(value) && value >= state.spreadMinBps &&
            (!state.spreadMaxBps || value <= state.spreadMaxBps);
    }
    function syncSpreadFilterControls() {
        const min = $('v2-spread-min'), max = $('v2-spread-max'), hint = $('v2-spread-filter-hint');
        if (min) min.value = (state.spreadMinBps / 100).toFixed(4).replace(/0+$/, '').replace(/\.$/, '') || '0';
        if (max) max.value = state.spreadMaxBps ? (state.spreadMaxBps / 100).toFixed(4).replace(/0+$/, '').replace(/\.$/, '') : '0';
        if (hint) hint.textContent = state.spreadMaxBps
            ? `当前筛选 ${(state.spreadMinBps / 100).toFixed(4)}% 至 ${(state.spreadMaxBps / 100).toFixed(4)}%；实盘候选遵循此范围`
            : `最低 ${(state.spreadMinBps / 100).toFixed(4)}%，最高不限；实盘候选遵循此范围`;
        const summary = $('v2-opportunity-filter-summary');
        if (summary) summary.textContent = `${state.spreadMinBps ? '毛价差 ≥ ' + (state.spreadMinBps / 100).toFixed(2) + '%' : '毛价差不限'}${state.spreadMaxBps ? '，≤ ' + (state.spreadMaxBps / 100).toFixed(2) + '%' : ''} · 历史中心 · 次数不限 · 时间不限 · 样本不限`;
    }
    function spreadQuery() {
        // Fetch both directions from the same existing quote batch. Apply
        // display filters locally so a rejected counterpart stays explainable.
        return '&gross_min_bps=0&gross_max_bps=0' +
            '&target_mode=' + encodeURIComponent(state.reversionTargetMode) +
            '&target_window_seconds=' + encodeURIComponent(state.reversionWindow);
    }
    function reversionKey(row) {
        return (row?.canonical_symbol || '') + '|' + row?.buy_venue + '->' + row?.sell_venue;
    }
    function reversionFilterActive() {
        return state.reversionMinCount > 0 || state.reversionMinDeviationBps > 0 || state.reversionMaxAge > 0 || state.reversionMinSamples > 0 || state.reversionTargetMode !== 'center';
    }
    function reversionAllowed(row) {
        if (!reversionFilterActive()) return true;
        const stats = state.reversionStats.get(reversionKey(row));
        if (!stats || stats.statistics_ready === false) return false;
        if (Number(stats.samples || 0) < state.reversionMinSamples) return false;
        if (Number(stats.reversion_count || 0) < state.reversionMinCount) return false;
        const elapsed = Math.max(0, (Date.now() - Number(stats._loaded_at ?? Date.now())) / 1000);
        if (state.reversionRecencyMode === 'hard' && state.reversionMaxAge > 0 && (stats.last_reversion_age_seconds == null || Number(stats.last_reversion_age_seconds) + elapsed > state.reversionMaxAge)) return false;
        return true;
    }
    function reversionSummary(row) {
        const stats = state.reversionStats.get(reversionKey(row));
        if (!stats || stats.statistics_ready === false) return reversionFilterActive() ? '统计读取中' : '点击查看';
        const count = Number(stats.reversion_count || 0);
        const target = stats.target_mode === 'zero' ? '回0' : stats.target_mode === 'either' ? '中心/0' : '中心';
        return count ? `${target} ${count} 次 · 上次 ${elapsedLabel(stats.last_reversion_age_seconds)}` : `暂无${target}回归`;
    }
    function recencyRank(row) {
        if (state.reversionRecencyMode !== 'soft' || state.reversionMaxAge <= 0) return 0;
        const stats = state.reversionStats.get(reversionKey(row));
        if (stats?.statistics_ready === false || stats?.last_reversion_age_seconds == null) return 0;
        const elapsed = Math.max(0, (Date.now() - Number(stats._loaded_at ?? Date.now())) / 1000);
        const age = Number(stats.last_reversion_age_seconds) + elapsed;
        return age >= 0 && age <= state.reversionMaxAge ? 1 : 0;
    }
    function opportunityRank(row) {
        if (row.direction_filter_reason) return [-1];
        const preview = row?.return_preview;
        if (preview?.net_bps != null && Number.isFinite(Number(preview.net_bps)))
            return [preview.meets_floor ? 3 : 2, Number(preview.profit_usdc), -Math.max(row.buy_age_seconds ?? Infinity, row.sell_age_seconds ?? Infinity), Number(preview.net_bps), recencyRank(row)];
        const estimate = row?.estimated_reversion_profit_usdc == null ? NaN : Number(row.estimated_reversion_profit_usdc);
        const net = row?.net_bps == null ? NaN : Number(row.net_bps);
        const gross = Number(row?.gross_bps || 0);
        if (Number.isFinite(estimate) && estimate > 0) return [2, estimate, Number.isFinite(net) ? net : -Infinity, recencyRank(row), gross];
        if (Number.isFinite(net)) return [1, net, recencyRank(row), gross];
        return [0, gross, recencyRank(row)];
    }
    function compareOpportunity(left, right) {
        const a = opportunityRank(left), b = opportunityRank(right);
        for (let index = 0; index < Math.max(a.length, b.length); index += 1) {
            const av = Number.isFinite(a[index]) ? a[index] : -Infinity;
            const bv = Number.isFinite(b[index]) ? b[index] : -Infinity;
            if (av !== bv) return bv - av;
        }
        return 0;
    }
    function refreshReversionEstimate(row) {
        // Every REST/WS tick carries a fresh estimate from the same Python
        // model as execution. Never recalculate from an old center or inherit
        // an estimate after the user changes target/window.
        const preview = row.return_preview || {};
        const basis = window.StrategyChannelUI?.basis();
        const matching = preview.target_mode === 'channel'
            ? (!basis || basis.mode==='channel' && Object.entries(basis.policy).every(([key,value])=>preview.channel_policy?.[key]===value))
            : basis?.mode!=='channel' && preview.requested_target_mode === state.reversionTargetMode && Number(preview.window_seconds) === state.reversionWindow;
        const updated = {...row, estimated_reversion_profit_usdc:null};
        if (!matching) {
            updated.return_preview = {reason:'目标或统计窗口已切换，等待本次目标的新估算'};
            updated.favorable_deviation_bps = null;
            return updated;
        }
        const ages = freshness.describe(row).ages || [row.buy_age_seconds, row.sell_age_seconds];
        if (row.buy_stale || row.sell_stale || ages.some(value => value != null && Number(value) > 5)) {
            updated.return_preview = {...preview,net_bps:null,profit_usdc:null,meets_floor:false,reason:'报价超过实盘估算时效，等待新盘口'};
            return updated;
        }
        if (!row.buy_stale && !row.sell_stale) updated.estimated_reversion_profit_usdc = preview.profit_usdc ?? null;
        return updated;
    }
    function reversionEstimate(row) {
        const p = row?.return_preview || {};
        if (p.profit_usdc == null || row?.estimated_reversion_profit_usdc == null) return '—';
        const target = p.target_mode === 'zero' ? '回0' : '中心';
        const profit = Number(p.profit_usdc);
        return profit.toFixed(Math.abs(profit) < 1 ? 4 : 2) + ' USDC';
    }
    function reversionEstimateDetail(row) {
        const p = row?.return_preview || {};
        if (p.net_bps == null) return p.reason || '整轮成本未齐，缺失项保持未知';
        const c = p.components;
        const labels = c ? {target_remaining_bps:'目标残余价差（不是收费）',close_quote_width_bps:'预计平仓买卖点差',
            fees_bps:'开平仓手续费',book_impact_bps:'同数量盘口冲击估算',
            movement_budget_bps:Number(p.cost_model_version)>=2?'预计两笔平仓价格变动预算':'四次成交价格变动预算',
            funding_cost_bps:p.funding?.mode==='dynamic'?'预计资金费净支出':'固定/兜底资金费预算',funding_extra_bps:'资金费额外预留'} :
            {open_fees_bps:'开仓手续费',open_impact_and_movement_bps:'开仓冲击及价格预算',
            target_and_close_width_bps:'目标及平仓买卖点差',close_impact_and_movement_bps:'平仓冲击及价格预算',
            close_fees_bps:'平仓手续费',funding_bps:'资金费预算'};
        const amounts = c || p.costs || {};
        const numeric = value => value != null && Number.isFinite(Number(value));
        const fixed = value => (Math.abs(Number(value))<1e-8?0:Number(value)).toFixed(4);
        const costs = Object.entries(amounts).map(([key,value]) =>
            (labels[key] || key)+' '+(numeric(value)?Number(value).toFixed(2)+' bps':'未知')).join('；');
        const required = c ? ['target_remaining_bps','close_quote_width_bps','fees_bps','book_impact_bps',
            'movement_budget_bps','funding_cost_bps','funding_extra_bps'] :
            ['open_fees_bps','open_impact_and_movement_bps','target_and_close_width_bps',
             'close_impact_and_movement_bps','close_fees_bps','funding_bps'];
        const complete = numeric(p.gross_bps) && required.every(key=>numeric(amounts[key]));
        const equation = complete ? '整轮净价差 = 毛价差 − '+required.map(key=>labels[key]).join(' − ')+
            '\n代入（bps）：'+fixed(p.gross_bps)+required.map(key=>
                (Number(amounts[key])<0?' + ':' − ')+fixed(Math.abs(Number(amounts[key])))).join('')+
            ' = '+fixed(p.net_bps)+' bps（'+bpsPercent(p.net_bps)+'；显示值已四舍五入）。' :
            '完整数值算式待同轮报价补齐；以下仅列已提供的成本项。';
        const funding = p.funding || {};
        const fundingDetail = funding.reason ? '资金费：'+funding.reason+
            (funding.mode==='dynamic'?'；预计持仓 '+Number(funding.horizon_seconds)/60+' 分钟；'+
             (funding.legs||[]).map(leg=>(leg.side==='long'?'多腿':'空腿')+' '+Number(leg.rate_bps).toFixed(3)+' bps/次 × '+leg.settlements+' 次').join('，')+'；未来小时沿用本次预测，非实际扣费。':'。') : '';
        const evidence = p.movement_evidence;
        const movementDetail = '每腿下单价格保护 / 预计平仓变价预算 '+(numeric(p.movement_bps)?Number(p.movement_bps).toFixed(2)+' bps':'未知')+
            (evidence?.observation_only?'（按配置；历史窗口仅观察，不加预算，同路线近24小时 '+evidence.samples+' 次）':
             evidence?'（同路线近24小时 '+evidence.samples+' 次；'+(Number(evidence.samples)>=5?
                '配置基础与实测 P90 '+Number(evidence.movement_bps).toFixed(2)+' bps 取大值':'样本不足5次，尚不能按实测 P90 校准')+'）':'')+'。'+
            (Number(p.cost_model_version)>=2?'开仓固定变价扣减为0，由入场预留覆盖；真实开仓盘口冲击仍计入。':'');
        const reserve = p.entry_reserve_bps ?? p.policy?.entry_reserve_bps;
        const threshold = numeric(reserve) && numeric(p.minimum_bps)
            ? '入场门槛：'+fixed(Number(p.minimum_bps)-Number(reserve))+'（有效净收益底线） + '+fixed(reserve)+
                '（入场预留） = '+fixed(p.minimum_bps)+' bps；'+
                (p.meets_floor?'本次估算达标，仍待提交前核验。':'本次'+p.reason+'。')
            : '入场门槛 '+Number(p.minimum_bps).toFixed(2)+' bps（含入场预留）；'+p.reason+'。';
        const cash = numeric(p.reference_notional) && numeric(p.profit_usdc)
            ? '预估净收益：'+fixed(p.net_bps)+' ÷ 10000 × '+Number(p.reference_notional).toFixed(6)+
                ' U = '+Number(p.profit_usdc).toFixed(6)+' U。' : '';
        const prices = numeric(row.buy_price) && numeric(row.sell_price)
            ? '毛价差按（卖出 Bid '+row.sell_price+' − 买入 Ask '+row.buy_price+'）÷ 较大开仓价格 × 10000 计算。' : '';
        const cs = p.center_statistics;
        const centerDetail = cs ? '\n中心质量：'+(cs.center_reason || cs.center_status || '统计中')+
            '；有效样本 '+(cs.samples || 0)+' / 原始 '+(cs.raw_samples || 0)+
            '；报价失效 '+(cs.invalid_quote_samples || 0)+'；覆盖 '+Math.round(Number(cs.sample_span_seconds || 0)/60)+' 分钟。'+
            Object.entries(cs.recent_centers || {}).map(([window,value])=>'\n近 '+Number(window)/3600+' 小时中心 '+
                bpsPercent(value.center_bps)+'（'+value.samples+' 点）').join('') : '';
        return equation+'\n'+prices+'\n'+(p.target_mode==='channel'?'按已保存通道换算的方向目标':p.target_mode==='zero'?'按筛选目标 回0':'按筛选目标 历史中心')+' '+bpsPercent(p.target_bps)+
            '，近 '+Number(p.window_seconds)/86400+' 天；同数量 '+Number(p.quantity).toPrecision(6)+
            '，参考名义金额 '+Number(p.reference_notional).toFixed(2)+' U。\n'+costs+
            '。\n'+cash+'\n'+threshold+centerDetail+'\n'+fundingDetail+movementDetail+
            '入场预留只提高门槛，不再扣一次成本。平仓盘口与变价为情景估算，不是未来实际成交。实盘仍核验数量精度、账户和最新同数量报价。';
    }
    function syncReturnBasis() {
        const node = $('v2-return-basis');
        if (!node) return;
        const labels = {center:'历史中心',zero:'回归到0',either:'中心或0（选择最近有利目标）',channel:'入场冻结通道目标'};
        const basis = window.StrategyChannelUI?.basis();
        let text = basis?.mode==='channel'?'列表按已保存通道目标估算整轮收益；历史回归统计选项仅用于历史筛选。已有持仓沿用本单冻结目标。':
            '列表估算目标：'+labels[state.reversionTargetMode]+' · 近 '+state.reversionWindow/86400+' 天；已有持仓沿用本单冻结目标。';
        const execution = state.executionContext;
        if (execution?.running && Number(execution?.position_monitor?.entry_scan_pipeline_version||0) < 3)
            text += ' 当前实盘进程尚未加载新版资金费核算及执行提速，下次启动生效。';
        if (execution?.running && Number(execution?.position_monitor?.entry_profit_model_version||0) < 2)
            text += ' 当前实盘仍使用旧开仓成本算法；取消开仓固定变价重复扣减需下次启动实盘生效。';
        const live = execution?.last_quote_evaluation?.detail?.target_context;
        if (execution?.running && live) {
            text += ' 后台本轮回归目标：'+labels[live.target_mode]+' · 近 '+Number(live.window_seconds)/86400+' 天。';
            if (basis?.mode==='channel' ? live.target_mode!=='channel' : (live.target_policy_mode || live.target_mode) !== state.reversionTargetMode || Number(live.window_seconds) !== state.reversionWindow)
                text += ' 列表筛选与后台目标不同；修改列表不会改动已运行策略或已有持仓的冻结目标。';
        }
        node.textContent = text;
    }
    window.updateOpportunityExecutionContext = execution => { state.executionContext = execution; syncReturnBasis(); };
    function syncReversionFilterControls() {
        syncReturnBasis();
        const windowNode = $('v2-reversion-window');
        const countNode = $('v2-reversion-min-count');
        const deviationNode = $('v2-reversion-min-deviation');
        const ageNode = $('v2-reversion-max-age');
        const recencyNode = $('v2-reversion-recency-mode');
        const sampleNode = $('v2-reversion-min-samples');
        const targetNode = $('v2-reversion-target');
        const hint = $('v2-reversion-filter-hint');
        const summary = $('v2-opportunity-filter-summary');
        if (windowNode) windowNode.value = String(state.reversionWindow);
        if (countNode) countNode.value = String(state.reversionMinCount);
        if (deviationNode) deviationNode.value = String(state.reversionMinDeviationBps / 100 || 0);
        if (recencyNode) recencyNode.value = state.reversionRecencyMode;
        if (ageNode) ageNode.value = String(state.reversionMaxAge / 3600 || 0);
        if (sampleNode) sampleNode.value = String(state.reversionMinSamples);
        if (targetNode) targetNode.value = state.reversionTargetMode;
        if (summary) {
            const target = state.reversionTargetMode === 'zero' ? '回 0' : state.reversionTargetMode === 'either' ? '中心或 0' : '历史中心';
            const count = state.reversionMinCount ? `至少 ${state.reversionMinCount} 次` : '次数不限';
            const deviation = state.reversionMinDeviationBps ? `偏离≥ ${(state.reversionMinDeviationBps / 100).toFixed(2)}%` : '偏离不限';
            const age = state.reversionMaxAge ? (state.reversionRecencyMode === 'soft' ? `软排序：近 ${state.reversionMaxAge / 3600} 小时优先` : `硬过滤：上次 ≤ ${state.reversionMaxAge / 3600} 小时`) : '时间不限';
            const samples = state.reversionMinSamples ? `样本 ≥ ${state.reversionMinSamples}` : '样本不限';
            const spread = (state.spreadMinBps ? '毛价差 ≥ ' + (state.spreadMinBps / 100).toFixed(2) + '%' : '毛价差不限') + (state.spreadMaxBps ? '，≤ ' + (state.spreadMaxBps / 100).toFixed(2) + '%' : '');
            summary.textContent = `${target} · ${deviation} · ${count}`;
            summary.title = `${spread} · ${target} · ${deviation} · ${count} · ${age} · ${samples}`;
        }
        if (hint) {
            const target = state.reversionTargetMode === 'zero' ? '回到 0' : state.reversionTargetMode === 'either' ? '回到历史中心或 0' : '回到历史中心';
            if (!reversionFilterActive()) hint.textContent = '未启用回归筛选；点击机会详情仍可查看近几天的每次回归。';
            else hint.textContent = `按${target}统计；只有先偏离至少 ${state.reversionMinDeviationBps ? (state.reversionMinDeviationBps / 100).toFixed(2) + '%' : '不限'} 才计为一次回归；近 ${state.reversionWindow / 86400} 天内至少 ${state.reversionMinCount || 0} 次回归` + (state.reversionMaxAge ? (state.reversionRecencyMode === 'soft' ? `；软排序：同等收益下，近 ${elapsedLabel(state.reversionMaxAge)} 回归优先，超时仍核验收益` : ` 且距上次不超过 ${elapsedLabel(state.reversionMaxAge)}`) : '；距上次回归不限') + (state.reversionMinSamples ? `；历史样本至少 ${state.reversionMinSamples}` : '') + '；读取已有历史样本，列表和全品种执行候选都会同步筛选。';
        }
    }
    function reversionBaseRows(rows) {
        // Check both routes before choosing a display direction. A route with
        // better current prices may fail the historical filter while its
        // opposite route passes it. Apply the API batch cap after cache lookup
        // so catalogs larger than 80 routes cannot starve the remaining routes.
        return [...new Map((rows || []).map(row => [reversionKey(row), row])).values()];
    }
    async function ensureReversionStats(rows) {
        if (!reversionFilterActive() || state.reversionLoading) return;
        const candidates = reversionBaseRows(rows);
        const now = Date.now();
        const missing = candidates.filter(row => {
            const key = reversionKey(row);
            const stats = state.reversionStats.get(key);
            const ttl = stats?.statistics_ready === false ? 1000 : 30000;
            return (!stats || now - Number(stats._loaded_at ?? 0) >= ttl) &&
                now - Number(state.reversionAttempts.get(key) ?? -Infinity) >= ttl;
        }).slice(0, 80);
        if (!missing.length) return;
        missing.forEach(row => state.reversionAttempts.set(reversionKey(row), now));
        state.reversionLoading = true;
        const requestId = ++state.reversionRequestId;
        try {
            const items = missing.map(row => symbolBase(row.canonical_symbol) + '|' + row.buy_venue + '->' + row.sell_venue).join(',');
            const stats = await apiFetch('/reversion-stats-batch?items=' + encodeURIComponent(items) + '&seconds=' + state.reversionWindow + '&band_bps=2&min_deviation_bps=' + encodeURIComponent(state.reversionMinDeviationBps) + '&target_mode=' + encodeURIComponent(state.reversionTargetMode), 12000);
            if (requestId !== state.reversionRequestId) return;
            (stats.items || []).forEach(item => state.reversionStats.set(item.symbol + '|' + item.pair_key, {...item, _loaded_at: Date.now()}));
        } catch (_) {
            // A failed diagnostic request must not stop the quote stream. Rows
            // remain visible after the user clears the optional filter.
        } finally {
            if (requestId === state.reversionRequestId) {
                state.reversionLoading = false;
                renderResult(visibleRows(state.allRows));
            }
        }
    }
    function elapsedLabel(seconds) {
        if (seconds == null || !Number.isFinite(Number(seconds))) return '暂无';
        const value = Number(seconds);
        if (value < 60) return Math.max(1, Math.round(value)) + ' 秒';
        if (value < 3600) return (value / 60).toFixed(1) + ' 分钟';
        if (value < 86400) return (value / 3600).toFixed(1) + ' 小时';
        return (value / 86400).toFixed(1) + ' 天';
    }
    function amountU(value) { return value == null ? '未知' : Number(value).toLocaleString('en-US', { maximumFractionDigits: 2 }) + ' U'; }
    function wearLabel(row) {
        if (row.wear_bps != null) return '入场 ' + bpsPercent(row.wear_bps);
        const fees = row.wear_breakdown?.fees_bps;
        return fees == null ? '手续费待核验' : '已知手续费 ' + Number(fees).toFixed(2) + ' bps';
    }
    function wearDetail(row) {
        const total = row.wear_bps == null ? '入场合计未齐，缺失成本不按零计算' : '入场合计 ' + bpsPercent(row.wear_bps);
        if (row.wear_summary) return total + '；' + row.wear_summary;
        const parts = row.wear_breakdown || {};
        const fees = parts.fees_bps == null ? '手续费未知' : '手续费 ' + Number(parts.fees_bps).toFixed(2) + ' bps';
        const slip = parts.slippage_bps == null ? '盘口滑点待测' : '盘口滑点 ' + Number(parts.slippage_bps).toFixed(2) + ' bps';
        return fees + '；' + slip + '；资金费未计；' + total;
    }
    function venueShort(value) {
        return ({ variational: 'Variational', lighter: 'Lighter', lighter_rh: 'Lighter RH', standx: 'StandX', binance: 'Binance', gate: 'Gate', okx: 'OKX', arcus: 'Arcus' })[value] || names(value);
    }
    function eventTime(timestamp) {
        if (timestamp == null) return '--';
        return new Date(Number(timestamp) * 1000).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
    }
    function renderReversionDetails(root, stats) {
        root.replaceChildren();
        const details = document.createElement('details');
        details.className = 'reversion-event-details';
        const summary = document.createElement('summary');
        summary.textContent = `查看每次回归详情（显示最近 ${(stats.events || []).length} / ${stats.reversion_count || 0} 次）`;
        details.appendChild(summary);
        const events = Array.isArray(stats.events) ? stats.events : [];
        if (!events.length) {
            const empty = document.createElement('p');
            empty.className = 'muted reversion-empty';
            empty.textContent = '当前时间范围内没有完整回归记录。';
            details.appendChild(empty);
        } else {
            const wrap = document.createElement('div'); wrap.className = 'reversion-event-table-wrap';
            const table = document.createElement('table'); table.className = 'reversion-event-table';
            table.innerHTML = '<thead><tr><th>偏离开始</th><th>回归时间</th><th>持续</th><th>从价差</th><th>回归价差</th><th>最大偏离</th><th>距今</th><th>距上次</th></tr></thead>';
            const body = document.createElement('tbody');
            events.forEach(event => {
                const row = document.createElement('tr');
                const endTimestamp = Number(event.timestamp);
                const startTimestamp = event.start_timestamp == null ? endTimestamp : Number(event.start_timestamp);
                const duration = event.duration_seconds == null ? Math.max(0, endTimestamp - startTimestamp) : Number(event.duration_seconds);
                const ageSeconds = Math.max(0, Math.floor(Date.now() / 1000) - endTimestamp);
                [eventTime(startTimestamp), eventTime(endTimestamp), elapsedLabel(duration), bpsPercent(event.start_spread_bps == null ? event.spread_bps : event.start_spread_bps), bpsPercent(event.spread_bps), bpsPercent(event.peak_deviation_bps), elapsedLabel(ageSeconds), elapsedLabel(event.interval_seconds)].forEach(value => { const cell = document.createElement('td'); cell.textContent = value; row.appendChild(cell); });
                body.appendChild(row);
            });
            table.appendChild(body); wrap.appendChild(table); details.appendChild(wrap);
        }
        const target = stats.target_mode === 'zero' ? '0' : stats.target_mode === 'either' ? '历史中心或 0' : '历史中心 ' + bpsPercent(stats.center_bps);
        const minDeviation = Number(stats.min_deviation_bps || 0);
        const threshold = minDeviation > 0 ? '；只有最大偏离达到 ' + bpsPercent(minDeviation) + ' 才计入回归' : '';
        const anomalyCount = Number(stats.anomaly_count || 0);
        const anomalyNote = anomalyCount > 0 ? '；另有 ' + anomalyCount + ' 个异常点仅保留在原始图表中，未计入统计' : '';
        const note = document.createElement('p'); note.className = 'muted reversion-event-note'; note.textContent = '一条记录表示：价差从“偏离开始”到“重新回到 ' + target + ' ±' + Number(stats.band_bps || 2).toFixed(1) + ' bps”这一段过程' + threshold + anomalyNote + '；这是历史诊断，不代表未来一定回归。'; details.appendChild(note);
        root.appendChild(details);
    }
    let reversionChartInstance = null, reversionChartRoot = null, reversionResizeObserver = null;
    function disposeReversionChart() {
        reversionResizeObserver?.disconnect(); reversionResizeObserver = null;
        reversionChartInstance?.dispose(); reversionChartInstance = null; reversionChartRoot = null;
    }
    const chartNumber = value => typeof value === 'number' && Number.isFinite(value);
    // Drawing preferences only. Never feed these values into strategy evaluation.
    const chartVisibilityKey = 'desk.detailChart.lines.v1';
    let chartVisibility = {};
    try {
        const saved = JSON.parse(localStorage.getItem(chartVisibilityKey) || '{}');
        if (saved && typeof saved === 'object' && !Array.isArray(saved)) chartVisibility = saved;
    } catch (_) { /* Storage can be disabled; keep choices in memory. */ }
    const chartLineVisible = key => chartVisibility[key] !== false;
    const chartMarkKey = mark => ({'上边界':'upper','冻结上界':'upper','下边界':'lower','冻结下界':'lower',
        '通道中心':'center','冻结中心':'center','历史中心':'center','临时中心·样本待就绪':'center',
        '本单冻结目标':'target','0':'zero','+回归带':'bandUpper','-回归带':'bandLower','P10':'p10','P90':'p90'})[mark.name];
    function chartLineControls(root, entries, redraw) {
        root._chartRedraw = redraw;
        const signature = entries.map(([key,label]) => key + ':' + label).join('|');
        if (!root._chartControls || root._chartControls.parentNode !== root) {
            const controls = document.createElement('details'); controls.className = 'chart-line-controls';
            const summary = document.createElement('summary'); controls.appendChild(summary);
            const options = document.createElement('div'); options.className = 'chart-line-options'; controls.appendChild(options);
            root.appendChild(controls); root._chartControls = controls; root._chartControlsTitle = summary; root._chartOptions = options;
            root._chartControlsSignature = null;
        }
        if (root._chartControlsSignature !== signature) {
            const options = root._chartOptions; options.replaceChildren(); root._chartInputs = new Map();
            const save = () => {
                try { localStorage.setItem(chartVisibilityKey, JSON.stringify(chartVisibility)); } catch (_) {}
                root._chartRedraw();
            };
            for (const [key, label] of entries) {
                const field = document.createElement('label'), input = document.createElement('input'), text = document.createElement('span');
                input.type = 'checkbox'; input.checked = chartLineVisible(key); input.setAttribute('aria-label', label);
                text.textContent = label; field.appendChild(input); field.appendChild(text); options.appendChild(field);
                root._chartInputs.set(key, input);
                input.addEventListener('change', () => { chartVisibility[key] = input.checked; save(); });
            }
            const actions = document.createElement('div'); actions.className = 'chart-line-actions';
            for (const [label, all] of [['仅策略线', false], ['全部显示', true]]) {
                const button = document.createElement('button'); button.type = 'button'; button.className = 'secondary compact-action'; button.textContent = label;
                button.addEventListener('click', () => {
                    for (const [key] of entries) chartVisibility[key] = all || ['spread','current','upper','lower','center','target'].includes(key);
                    save();
                });
                actions.appendChild(button);
            }
            options.appendChild(actions);
            const note = document.createElement('small'); note.textContent = '仅影响图表显示，选择自动记住。'; options.appendChild(note);
            root._chartControlsSignature = signature;
        }
        for (const [key, input] of root._chartInputs) input.checked = chartLineVisible(key);
        root._chartControlsTitle.textContent = '显示线条 · ' + entries.filter(([key]) => chartLineVisible(key)).length + '/' + entries.length;
    }
    function executionGross(row) {
        const a=row.buy_price, b=row.sell_price;
        return [a,b].every(v=>chartNumber(v)&&v>0) ? (b-a)/Math.max(a,b)*10000 : null;
    }
    function drawerChannelSpec(row, position) {
        const basis = window.StrategyChannelUI?.basis();
        const frozen = position?.entry_target?.channel;
        const enabled = position ? Boolean(frozen) : basis ? basis.mode === 'channel' : row.return_preview?.target_mode === 'channel';
        return {enabled, frozen, canonical:enabled || !position, axis: enabled || !position ? [...(frozen?.axis_venues || [row.buy_venue, row.sell_venue].sort())] : [row.buy_venue, row.sell_venue],
            policy: frozen?.policy || basis?.policy || row.return_preview?.channel_policy};
    }
    function drawerChannelState(row, spec) {
        if (!spec.enabled && !spec.canonical) return null;
        const preview = row.return_preview || {}, basis = window.StrategyChannelUI?.basis();
        const sameAxis = channel => Array.isArray(channel?.axis_venues) && channel.axis_venues.length === 2 &&
            channel.axis_venues.every((venue, i) => venue === spec.axis[i]) &&
            [row.buy_venue, row.sell_venue].every(venue => spec.axis.includes(venue));
        const matching = !basis || basis.mode === 'channel' &&
            Object.entries(basis.policy).every(([key, value]) => preview.channel_policy?.[key] === value);
        const candidate = spec.frozen || (matching && preview.target_mode === 'channel' ? preview.channel : null);
        const channel = sameAxis(candidate) && ['lower_bps', 'upper_bps', 'center_bps'].every(key => chartNumber(candidate[key])) &&
            candidate.lower_bps <= candidate.center_bps && candidate.center_bps <= candidate.upper_bps &&
            candidate.lower_bps < candidate.upper_bps ? candidate : null;
        const quoteState = freshness.describe(row);
        const ages = quoteState.ages || [row.buy_age_seconds, row.sell_age_seconds];
        const fresh = !quoteState.stale && !row.buy_stale && !row.sell_stale && ages.length === 2 &&
            ages.every(age => chartNumber(age) && age >= 0 && age <= 5);
        let current = null;
        if (fresh) {
            // The lower route needs A's ask and B's bid too. Negating the
            // reverse trade spread would discard both venues' bid/ask widths.
            const inputs = row.return_inputs || {};
            const upper = row.buy_venue === spec.axis[0];
            const ask = upper ? row.buy_price : inputs.sell?.ask;
            const bid = upper ? row.sell_price : inputs.buy?.bid;
            if ([ask, bid].every(value => chartNumber(value) && value > 0))
                current = (bid - ask) / Math.max(ask, bid) * 10000;
            else if (sameAxis(preview.channel) && chartNumber(preview.channel.entry_axis_bps))
                current = preview.channel.entry_axis_bps;
        }
        return {channel, current, frozen: Boolean(spec.frozen), axis: spec.axis, axisOnly:!spec.enabled,
            directions:row.pair_directions || {[row.buy_venue===spec.axis[0]?'forward':'reverse']:row},
            side: spec.frozen?.side || (row.buy_venue === spec.axis[0] ? 'upper' : 'lower'),
            reason: !spec.frozen && basis && !matching ? '策略已更新，等待当前配置的通道估算' : preview.reason,
            estimateReady: matching && preview.target_mode === 'channel' && preview.meets_floor === true};
    }
    function channelChartNote(context) {
        const ch = context.channel;
        const axis = 'A=' + venueShort(context.axis[0]) + ' / B=' + venueShort(context.axis[1]);
        if (context.axisOnly) return axis+' · 固定 A/B 盘口毛价差，未扣费用。';
        if (!ch) return axis + ' · 通道边界未就绪：' + (context.reason || '等待有效统计和当前策略估算') + '；不绘制推测边界。';
        const source = context.frozen ? '本单冻结通道' : '当前策略';
        if (context.current == null) return source + ' · ' + axis + ' · 当前报价缺失或超过 5 秒，暂不判断触线。';
        if (context.frozen) return source + ' · ' + axis + ' · 当前 ' + context.current.toFixed(2) + ' bps；触线后仍须净盈利及平仓核验达标。';
        const upper = context.side === 'upper', boundary = ch[upper ? 'upper_bps' : 'lower_bps'];
        const touched = upper ? context.current >= boundary : context.current <= boundary;
        if (!touched) return source + ' · ' + axis + ' · 距本方向' + (upper ? '上' : '下') + '边界 ' + Math.abs(boundary-context.current).toFixed(2) + ' bps。';
        return source + ' · ' + axis + ' · 本方向已触边；只读估算：' +
            (context.estimateReady ? '收益达标，仍待实盘复核（不代表已下单）' : context.reason || '等待同轮整轮收益估算') + '。';
    }
    function renderReversionChart(root, history, stats, currentPoint = null, referenceHistory = [], frozenTarget = null, channelContext = null) {
        let zoomState = null;
        if (reversionChartInstance) {
            const previous = reversionChartInstance.getOption?.()?.dataZoom;
            if (Array.isArray(previous)) zoomState = previous.map(item => ({ type: item.type, start: item.start, end: item.end, startValue: item.startValue, endValue: item.endValue, height: item.height, bottom: item.bottom }));
        }
        if (reversionChartRoot !== root) { disposeReversionChart(); root.replaceChildren(); }
        root.className = 'drawer-reversion-chart';
        // Stored ratio spreads use buy ask as denominator; the execution
        // channel uses max(ask,bid). Convert only the observations, never the
        // already evaluated execution boundaries or a frozen exit target.
        const axisValue = value => channelContext && value > 0 ? value / (1 + value / 10000) : value;
        const points = (Array.isArray(history) ? history : []).filter(item => item.timestamp != null && chartNumber(item.gross_bps))
            .map(item => ({...item, gross_bps:axisValue(item.gross_bps)}));
        if (!window.echarts || (!points.length && !currentPoint)) {
            disposeReversionChart(); root.replaceChildren();
            root.textContent = (!window.echarts ? '图表资源未加载；下方表格仍可查看回归明细。' : '暂无足够的历史价差样本绘图。') +
                (channelContext ? ' ' + channelChartNote(channelContext) : '');
            return;
        }
        const host = reversionChartRoot === root ? root._chartHost : document.createElement('div');
        if (!reversionChartInstance) {
            host.className = 'drawer-reversion-chart-host'; host.setAttribute('role', 'img'); host.setAttribute('aria-label', '历史价差与策略边界'); root.appendChild(host);
            root._chartHost = host; reversionChartRoot = root;
            reversionChartInstance = window.echarts.init(host, null, { renderer: 'canvas' });
            if (window.ResizeObserver) { reversionResizeObserver = new window.ResizeObserver(() => reversionChartInstance?.resize()); reversionResizeObserver.observe(host); }
        }
        const band = Math.abs(Number(stats.band_bps || 2));
        const statsValue = value => channelContext ? stats.spread_basis==='max_price' ? value : axisValue(value) : stats.spread_basis==='max_price' && value>0 ? value/(1-value/10000) : value;
        const center = channelContext && !channelContext.axisOnly ? channelContext.channel?.center_bps ?? null : chartNumber(stats.center_bps) ? statsValue(stats.center_bps) : null;
        const p10 = chartNumber(stats.p10_bps) ? statsValue(stats.p10_bps) : null;
        const p90 = chartNumber(stats.p90_bps) ? statsValue(stats.p90_bps) : null;
        const normalPoints = points.filter(item => item.anomalous !== true && item.valid_for_center !== false);
        const anomalyPoints = points.filter(item => item.anomalous === true);
        const values = normalPoints.map(item => [Number(item.timestamp) * 1000, Number(item.gross_bps)]);
        const currentValue = currentPoint && Number.isFinite(Number(currentPoint.gross_bps)) ? Number(currentPoint.gross_bps) : null;
        const currentTimestamp = currentPoint && Number.isFinite(Number(currentPoint.timestamp)) ? Number(currentPoint.timestamp) * 1000 : null;
        if (currentValue != null && currentTimestamp != null) values.push([currentTimestamp, currentValue]);
        const anomalyValues = anomalyPoints.map(item => [Number(item.timestamp) * 1000, Number(item.gross_bps)]);
        const referenceValues = (Array.isArray(referenceHistory) ? referenceHistory : [])
            .filter(item => item.timestamp != null && item.reference_bps != null)
            .map(item => [Number(item.timestamp) * 1000, axisValue(Number(item.reference_bps))]);
        const channel = channelContext?.channel;
        const marks = channelContext && !channelContext.axisOnly ? [
            ...(channel ? [
                {yAxis:channel.upper_bps, name:channelContext.frozen?'冻结上界':'上边界', lineStyle:{color:'#e9ad70',type:'dashed',width:1.5}},
                {yAxis:channel.lower_bps, name:channelContext.frozen?'冻结下界':'下边界', lineStyle:{color:'#8ca6ff',type:'dashed',width:1.5}},
                {yAxis:channel.center_bps, name:channelContext.frozen?'冻结中心':'通道中心', lineStyle:{color:'#f0bd62',type:'solid'}},
                ...(channelContext.frozen && chartNumber(channel.exit_axis_bps) ? [{yAxis:channel.exit_axis_bps,name:'本单冻结目标',lineStyle:{color:'#ff879b',type:'dashed',width:1.6},label:{position:'insideStartTop'}}] : [])
            ] : []),
            ...(channel && Math.abs(channel.center_bps) < 1e-9 ? [] : [{yAxis:0,name:'0',lineStyle:{color:'#314762',type:'solid'}}])
        ].map(mark=>({...mark,label:{position:'insideEndTop',...mark.label,formatter:()=>mark.name+' '+mark.yAxis.toFixed(2)+' bps',color:mark.lineStyle.color}})) : null;
        const allMarks = marks || [
            ...(center == null ? [] : [{ yAxis: center, name: stats.center_ready === false ? '临时中心·样本待就绪' : '历史中心', lineStyle: { color: '#f0bd62', type: 'solid', width: 1.2 } }]),
            ...(Number.isFinite(frozenTarget) ? [{ yAxis: frozenTarget, name: '本单冻结目标', label: {position:'insideStartTop',formatter:'本单冻结目标 {c} bps',color:'#ff879b'}, lineStyle: { color: '#ff879b', type: 'dashed', width: 1.6 } }] : []),
            { yAxis: 0, name: '0', lineStyle: { color: '#314762', type: 'solid' } },
            ...(center == null ? [] : [{ yAxis: center + band, name: '+回归带' }, { yAxis: center - band, name: '-回归带' }]),
            ...(p10 == null ? [] : [{ yAxis: p10, name: 'P10', lineStyle: { color: '#8fa6c0', type: 'dotted' } }]),
            ...(p90 == null ? [] : [{ yAxis: p90, name: 'P90', lineStyle: { color: '#8fa6c0', type: 'dotted' } }]),
        ];
        const entries = [['spread','盘口价差'], ['current','当前报价点'],
            ...allMarks.map(mark => [chartMarkKey(mark), mark.name === '0' ? '零轴' : mark.name]),
            ...(anomalyValues.length ? [['anomalies','异常点']] : []),
            ...(referenceValues.length ? [['reference','K 线参考']] : []),
            ...(!channelContext && stats.events?.length ? [['events','偏离区间阴影']] : [])];
        chartLineControls(root, entries, () => renderReversionChart(root, history, stats, currentPoint, referenceHistory, frozenTarget, channelContext));
        const compact = host.clientWidth > 0 && host.clientWidth < 420;
        reversionChartInstance.setOption({
            animation: false,
            // Reserve room for the y-axis and the end labels of the reference
            // lines.  With a narrow right margin ECharts clips the last digit.
            grid: { left: compact ? 22 : 50, right: compact ? 46 : 76, top: 28, bottom: 54, containLabel: true },
            tooltip: { trigger: 'axis', renderMode:'richText', valueFormatter: value => bpsPercent(value), axisPointer: { type: 'cross' },
                formatter: params => {
                    const visible = params.filter(p => p.seriesIndex !== 0 || chartLineVisible('spread'));
                    const point = visible.find(p => p.seriesIndex===0) || visible[0], value=point?.value;
                    if (!Array.isArray(value)) return '';
                    let text = new Date(value[0]).toLocaleTimeString('zh-CN',{hour12:false})+'\n'+(point.seriesIndex===0?'固定 A/B 价差 ':point.seriesName+' ')+Number(value[1]).toFixed(2)+' bps';
                    if (point.seriesIndex===0 && channelContext && value[0]===currentTimestamp) {
                        for (const [side,label] of [['forward','A买 / B卖'],['reverse','B买 / A卖']]) {
                            const row=channelContext.directions?.[side], current=row ? refreshReversionEstimate(row) : null;
                            text+='\n'+label+'：'+netBps(current)+(current ? ' · '+opportunityStatus(current.return_preview||{}) : ' · 等待本方向报价');
                        }
                    } else text+='\n历史报价点；双向整轮收益仅展示当前同轮估算';
                    return text;
                } },
            xAxis: { type: 'time', splitNumber:compact ? 3 : 6, axisLabel: { color: '#8195ad', fontSize: 9, hideOverlap:true, formatter: value => { const date = new Date(value); const pad = n => String(n).padStart(2, '0'); return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`; } }, axisLine: { lineStyle: { color: '#314762' } } },
            yAxis: { type: 'value', name: 'bps', nameTextStyle: { color: '#8195ad', fontSize: 9 }, axisLabel: { color: '#8195ad', fontSize: 9, formatter: value => Number(value).toFixed(1) }, splitLine: { lineStyle: { color: '#1e3046' } } },
            dataZoom: zoomState || [{ type: 'inside', filterMode: 'none' }, { type: 'slider', height: 16, bottom: 10, borderColor: '#314762', backgroundColor: '#0b1726', fillerColor: '#285349', handleStyle: { color: '#55d9ba' }, textStyle: { color: '#8195ad', fontSize: 9 } }],
            // Retain invisible anchor observations so ECharts still lays out
            // standalone strategy marks when the price/reference curves are hidden.
            series: [{ type: 'line', name: '正常价差', showSymbol: false, smooth: false,
                lineStyle: { color: '#55d9ba', width: 1.5, opacity: chartLineVisible('spread') ? 1 : 0 },
                emphasis: {disabled: !chartLineVisible('spread')}, itemStyle: { color: '#55d9ba' }, data: values,
                markLine: { silent: true, symbol: 'none', label: { show: true, position: 'end', distance: 8, color: '#b6c4d5', fontSize: 9 }, lineStyle: { color: '#657b91', type: 'dashed' }, data: allMarks.filter(mark => chartLineVisible(chartMarkKey(mark))) },
                markArea: { silent: true, itemStyle: { color: '#e6b45e12' }, data: (channelContext || !chartLineVisible('events') ? [] : stats.events || []).map(event => [{ xAxis: Number(event.start_timestamp == null ? event.timestamp : event.start_timestamp) * 1000 }, { xAxis: Number(event.timestamp) * 1000 }]) },
                markPoint: currentValue == null || !chartLineVisible('current') ? undefined : { silent: true, symbol: 'circle', symbolSize: 8, itemStyle: { color: '#f0bd62' }, label: { show: true, formatter: '当前', position:'right', color: '#f0bd62', fontSize: 9 }, data: [{ coord: [currentTimestamp, currentValue] }] },
            }, ...(anomalyValues.length && chartLineVisible('anomalies') ? [{ type: 'scatter', name: '异常点（不计入回归）', symbolSize: 8, itemStyle: { color: '#ff879b', borderColor: '#fff0f3', borderWidth: 1 }, data: anomalyValues }] : []),
                ...(referenceValues.length && chartLineVisible('reference') ? [{ type: 'line', name: '交易所历史参考价差（K线）', showSymbol: false, smooth: false, lineStyle: { color: '#8da9f2', width: 1.2, type: 'dashed' }, itemStyle: { color: '#8da9f2' }, data: referenceValues }] : [])],
        }, {notMerge:true,lazyUpdate:true});
        if (!root._chartHelp || root._chartHelp.parentNode !== root) {
            const help = document.createElement('details'); help.className='desk-disclosure';
            const title = document.createElement('summary'); title.textContent='图表说明';
            const legend = document.createElement('p'); legend.className='reversion-chart-legend';
            help.appendChild(title); help.appendChild(legend); root.appendChild(help);
            const note = document.createElement('p'); note.className='chart-note'; root.appendChild(note);
            root._chartHelp=help; root._chartLegend=legend; root._chartNote=note;
        }
        root._chartLegend.textContent = channelContext
            ? '绿色为固定 A Ask / B Bid 坐标的盘口毛价差：(B Bid − A Ask) / max(A Ask, B Bid) × 10000；未扣费用，不代表指定数量可成交。上边界方向为买 A 卖 B，下边界方向为买 B 卖 A，反向不是简单取负。边界直接复用后台执行模型，持仓使用入场冻结值。蓝色虚线为 K 线参考，不用于判断触边。滚轮、底部滑块可缩放；拖动可平移。'
            : '横轴显示到秒；滚轮或底部滑块可缩放，拖动图表可平移。绿色线按买入 Ask、卖出 Bid 绘制毛价差，未扣整轮成本，也不代表指定数量可成交；蓝色虚线是交易所 K 线收盘价重建的参考价差（不可执行）。红点是异常价差（保留显示但不计入统计），黄色点是最近一次实时毛价差。中心、分位线和回归带都不是实盘入场门槛，入场需核验整轮净收益。阴影表示价差偏离持续区间。';
        root._chartNote.textContent = channelContext ? channelChartNote(channelContext)
            : '盘口毛价差未扣费用；触线后仍须净盈利及平仓核验达标。';
    }
    function executionHistoryRow(position) {
        const firstVenue = position?.variational_venue || 'variational';
        const secondVenue = position?.lighter_venue || 'lighter';
        const firstSide = String(position?.variational_side || '').toLowerCase();
        const secondSide = String(position?.lighter_side || '').toLowerCase();
        const firstBuy = firstSide === 'long' || firstSide === 'buy';
        const buyVenue = firstBuy ? firstVenue : secondVenue;
        const sellVenue = firstBuy ? secondVenue : firstVenue;
        const buyPrice = Number(firstBuy ? position?.entry_price_variational : position?.entry_price_lighter);
        const sellPrice = Number(firstBuy ? position?.entry_price_lighter : position?.entry_price_variational);
        const buySize = Number(firstBuy ? (position?.entry_size_variational ?? position?.variational_size) : (position?.entry_size_lighter ?? position?.lighter_size));
        const sellSize = Number(firstBuy ? (position?.entry_size_lighter ?? position?.lighter_size) : (position?.entry_size_variational ?? position?.variational_size));
        const grossBps = Number.isFinite(buyPrice) && buyPrice > 0 && Number.isFinite(sellPrice)
            ? (sellPrice / buyPrice - 1) * 10000
            : Number(position?.actual_entry_spread_bps);
        const notionals = [buyPrice * buySize, sellPrice * sellSize].filter(value => Number.isFinite(value) && value >= 0);
        return {
            canonical_symbol: position?.symbol || '',
            buy_venue: buyVenue, sell_venue: sellVenue,
            buy_price: Number.isFinite(buyPrice) ? buyPrice : null,
            sell_price: Number.isFinite(sellPrice) ? sellPrice : null,
            gross_bps: Number.isFinite(grossBps) ? grossBps : null,
            net_bps: null, max_notional_usdc: notionals.length ? Math.min(...notionals) : null,
            buy_stale: false, sell_stale: false, executable: false,
            history_position: position
        };
    }
    function openHistoryDrawer(position) { openDrawer(executionHistoryRow(position)); }
    window.openOpportunityHistoryDrawer = openHistoryDrawer;
    window.updateOpportunityHistoryPositions = positions => drawerHistoryUpdate?.(positions);
    function openDrawer(row) {
        if (!row.history_position) row = refreshReversionEstimate(row);
        const drawer = $('opportunity-drawer'), title = $('opportunity-drawer-title'), body = $('opportunity-drawer-body');
        if (!drawer || !title || !body) return;
        if (drawerLiveTimer) { clearInterval(drawerLiveTimer); drawerLiveTimer = null; }
        disposeReversionChart();
        const titleAxis = row.history_position?.entry_target?.channel?.axis_venues || [row.buy_venue,row.sell_venue].sort();
        title.textContent = (row.canonical_symbol || '未知品种').replace(/-PERP$/, '') + ' · A=' + venueShort(titleAxis[0])+' / B='+venueShort(titleAxis[1])+' · '+venueShort(row.buy_venue)+'买 / '+venueShort(row.sell_venue)+'卖' + (row.history_position ? ' · 执行历史' : '');
        const trendAxis = !row.history_position || row.history_position.entry_target?.channel
            ? titleAxis : [row.buy_venue,row.sell_venue];
        window.setHistoryPair?.(trendAxis[0], trendAxis[1], row.canonical_symbol);
        drawerHistoryUpdate = null;
        drawerQuoteUpdate = null;
        body.replaceChildren();
        const metricNodes = new Map();
        const grid = document.createElement('div'); grid.className = 'drawer-metrics';
        const history = row.history_position;
        const valuationNote = document.createElement('p'); valuationNote.className = 'chart-note drawer-valuation-note';
        const valuationText = position => (position.mark_note || '浮动盈亏按双腿盘口中间价估算，已扣已知入场手续费，未扣未来平仓费和待结算资金费。') +
            ' ' + (globalThis.positionMonitorRowText?.(position.position_monitor) || '') + ' ' +
            (position.exit_estimate_note || '平仓估算未计额外盘口冲击与待结算资金费。');
        if (history) valuationNote.textContent = valuationText(history);
        const channelSpec = drawerChannelSpec(row, history);
        const historyClosed = history && (history.status === 'closed' || history.row_state === 'closed' || history.row_state === 'attention' && history.status === 'closed');
        const exitMetrics = position => {
            const summary = globalThis.positionMonitorExitSummary?.(position.position_monitor);
            return {
                pnl: summary ? Number(position.position_monitor.exit_net_pnl_usdc).toFixed(5) + ' U（价格保护后）'
                    : Number.isFinite(position.estimated_exit_pnl_usdc) ? position.estimated_exit_pnl_usdc.toFixed(5) + ' U（盘口估算，未扣保护预算）' : '测算待更新',
                state: summary?.text || globalThis.positionMonitorBrief?.(position.position_monitor) || '等待新鲜报价及账务核验',
                detail: summary?.title || position.exit_estimate_note || '盘口估算供参考；自动平仓仍须核验实际数量、费用及价格保护。'
            };
        };
        const targetNote = history?.entry_target?.channel
            ? '下图使用本单固定 A / B 坐标，显示入场时冻结的上下边界、中心和粉色退出目标；保存新策略不会移动这些线。平仓仍须通过实际净盈利及执行核验。'
            : history?.entry_target && Number.isFinite(history.entry_target.target_bps)
            ? '黄线是当前历史中心；粉色虚线是本单开仓时冻结的回归目标 ' + spreadText(history.entry_target.target_bps) + '。折线按原开仓方向的 Ask / Bid 绘制；平仓方向相反，还须通过实际平仓净盈利核验。'
            : '黄线是当前历史中心；折线按原开仓方向的 Ask / Bid 绘制。到达历史中心仍须核验反向平仓的可成交净收益。';
        const openMonitorStatus = position => !position.position_monitor ? '未平仓 · 监控心跳待接入' : position.position_monitor.heartbeat_stale ? '未平仓 · 监控心跳中断' : position.position_monitor.error ? '未平仓 · 平仓判断受阻' : position.position_monitor.account_status === 'pending' ? '未平仓 · 账户核验中' : '未平仓 · 持仓监控中';
        const historyStatus = history ? (history.hedge_status === 'single_leg_recovered' && historyClosed ? '已回补 · 未成对' : historyClosed ? (history.accounting_pending ? '已平仓 · 待核账' : '已平仓') : history.partially_closed ? '部分已平仓 · 待核对' : history.status === 'reconcile_required' ? '待核对 · 持仓复核受阻' : openMonitorStatus(history)) : null;
        const metrics = [
            ...(history ? [['执行状态', historyStatus], [historyClosed?'开仓数量（两腿）':'持仓数量（两腿）', (historyClosed ? (history.entry_size_variational ?? history.variational_size) : history.variational_size) + ' / ' + (historyClosed ? (history.entry_size_lighter ?? history.lighter_size) : history.lighter_size)], ['实际入场价差', spreadText(history.actual_entry_spread_bps)], ...(historyClosed ? [['平仓价（两腿）', (history.close_price_variational == null ? '--' : Number(history.close_price_variational).toLocaleString('en-US', {maximumFractionDigits: 8})) + ' / ' + (history.close_price_lighter == null ? '--' : Number(history.close_price_lighter).toLocaleString('en-US', {maximumFractionDigits: 8}))]] : []), [historyClosed ? '平仓价差' : '当前价差', !historyClosed && !Number.isFinite(history.current_spread_bps) ? (history.mark_error||'行情待更新') : spreadText(historyClosed ? history.close_spread_bps : history.current_spread_bps)], [historyClosed ? '净盈亏' : '浮动盈亏（估算）', historyClosed ? (history.realized_pnl_usdc == null ? (Number.isFinite(history.accounting_subtotal_usdc) ? '暂计 '+Number(history.accounting_subtotal_usdc).toFixed(6)+' U' : '缺成交明细') : Number(history.realized_pnl_usdc).toFixed(6) + ' U') : (history.unrealized_pnl_usdc == null ? (history.pnl_error||history.mark_error||'行情待更新') : Number(history.unrealized_pnl_usdc).toFixed(4) + ' U')]] : []),
            ...(history?.entry_target ? [['本单冻结目标',spreadText(history.entry_target.target_bps)]] : []),
            ...(history && !historyClosed ? [['持仓监控',globalThis.positionMonitorBrief?.(history.position_monitor)||'持仓监控心跳待接入'],['平仓净收益估算',exitMetrics(history).pnl],['平仓判断',exitMetrics(history).state]] : []),
            ...(historyClosed ? [['本单剩余数量（两腿）','0 / 0'],['成交来源',history.history_recovered?'平台历史成交与已确认回执自动补齐':'程序回执 / 平台历史'],['平仓时间（各腿）',Object.entries(history.close_fill_times||{}).map(([leg,stamp])=>venueShort(history[leg+'_venue'])+' '+new Date(stamp).toLocaleString('zh-CN',{hour12:false})+(history.close_fill_time_sources?.[leg]==='receipt_received'?'（回执收到时间）':'')).join(' / ')||'待补']] : []),
            ...(historyClosed && Number.isFinite(history.gross_price_pnl_usdc) ? [
                ['成交价差盈亏', Number(history.gross_price_pnl_usdc).toFixed(6)+' U'],
                ['已核验手续费', Number(history.known_fees_usdc).toFixed(6)+' U'],
                ['已核验资金费', Number(history.known_funding_usdc).toFixed(6)+' U'],
                ['核账说明', history.accounting_note||'费用与资金费已完整核算']
            ] : []),
            ['买入 Ask', row.buy_price == null ? '--' : Number(row.buy_price).toLocaleString('en-US', { maximumFractionDigits: 8 })],
            ['卖出 Bid', row.sell_price == null ? '--' : Number(row.sell_price).toLocaleString('en-US', { maximumFractionDigits: 8 })],
            ['毛价差', bpsPercent(executionGross(row))],
            ['开仓净差（未计平仓）', bpsPercent(row.net_bps)],
            ['整轮净价差', bpsPercent(row.return_preview?.net_bps)],
            ['预估回归净收益', reversionEstimate(row)],
            ['整轮入场门槛（含预留）', bpsPercent(row.return_preview?.minimum_bps)],
            ['首档容量估算（两腿等额）', amountU(row.max_notional_usdc)],
            ['磨损', wearDetail(row)],
            ['买入报价', freshness.describe(row).buyLabel],
            ['卖出报价', freshness.describe(row).sellLabel]
        ];
        metrics.forEach(([label, value]) => { const item = document.createElement('div'); item.innerHTML = '<span>' + label + '</span><b></b>'; const valueNode = item.querySelector('b'); valueNode.textContent = value; if (label === '整轮净价差') valueNode.title = reversionEstimateDetail(row); metricNodes.set(label, valueNode); grid.appendChild(item); });
        body.appendChild(grid);
        if (history) {
            const strategyButton = document.createElement('button');
            strategyButton.type = 'button'; strategyButton.className = 'secondary compact-action';
            strategyButton.textContent = '查看本单策略图';
            strategyButton.addEventListener('click',()=>window.StrategyChannelUI?.open(false, history));
            body.appendChild(strategyButton);
            const historyNote = document.createElement('p');
            historyNote.className = 'chart-note drawer-wear drawer-history-note';
            const closeReason = history.close_reason ? '；平仓原因：' + history.close_reason : '';
            const timing = history.execution_elapsed_ms == null ? '' : '；执行耗时 ' + durationText(history.execution_elapsed_ms);
            historyNote.textContent = '成交与净盈亏按实际成交核算；图表为当前行情参考。' + closeReason + timing;
            body.appendChild(historyNote);
            if (history.hedge_status === 'single_leg_recovered') {
                const explanation = document.createElement('p');
                explanation.className = 'chart-note drawer-wear drawer-rollback-explanation';
                explanation.style.whiteSpace = 'pre-line';
                explanation.textContent = history.rollback_explanation?.text || '旧记录缺少回补详情；具体数量和触发原因待核对。';
                body.appendChild(explanation);
            }
            const closeDiagnostic = document.createElement('p');
            if (history.hedge_status !== 'single_leg_recovered') {
                closeDiagnostic.className = 'chart-note drawer-wear drawer-close-diagnostics';
                closeDiagnostic.style.whiteSpace = 'pre-line';
                closeDiagnostic.textContent = history.close_diagnostics?.text || '未保存平仓决策快照；平台成交价不能还原触发时行情。';
                const review = document.createElement('details');
                review.className = 'desk-disclosure drawer-close-review';
                const summary = document.createElement('summary');
                summary.textContent = '平仓决策与成交对比';
                review.append(summary, closeDiagnostic);
                body.appendChild(review);
            }
            drawerHistoryUpdate = positions => {
                const current = positions.find(p => p.id === history.id && p.variational_venue === history.variational_venue && p.lighter_venue === history.lighter_venue);
                if (!current) return;
                if ((current.status === 'closed') !== Boolean(historyClosed)) { openHistoryDrawer(current); return; }
                if (historyClosed) {
                    const fields=['realized_pnl_usdc','accounting_subtotal_usdc','accounting_pending','close_spread_bps','close_price_variational','close_price_lighter','accounting_note','rollback_explanation','close_diagnostics'];
                    if(fields.some(key=>JSON.stringify(current[key])!==JSON.stringify(history[key])))openHistoryDrawer(current);
                    return;
                }
                const reason = current.mark_error || '行情待更新';
                closeDiagnostic.textContent = current.close_diagnostics?.text || '尚无已记录的平仓评估。';
                const last = current.last_estimate;
                metricNodes.get('当前价差').textContent = Number.isFinite(current.current_spread_bps) ? spreadText(current.current_spread_bps) : Number.isFinite(last?.spread_bps) ? '上次 '+spreadText(last.spread_bps)+' · 已失效' : reason;
                metricNodes.get('持仓监控').textContent = globalThis.positionMonitorBrief?.(current.position_monitor)||'持仓监控心跳待接入';
                metricNodes.get('平仓净收益估算').textContent = exitMetrics(current).pnl;
                metricNodes.get('平仓净收益估算').title = exitMetrics(current).detail;
                metricNodes.get('平仓判断').textContent = exitMetrics(current).state;
                metricNodes.get('浮动盈亏（估算）').textContent = Number.isFinite(current.unrealized_pnl_usdc) ? current.unrealized_pnl_usdc.toFixed(4) + ' U' : Number.isFinite(last?.unrealized_pnl_usdc)?'上次 '+last.unrealized_pnl_usdc.toFixed(4)+' U · 已失效':(current.pnl_error || reason);
                metricNodes.get('浮动盈亏（估算）').className = !Number.isFinite(current.unrealized_pnl_usdc)&&last?'history-estimate-stale':'';
                metricNodes.get('持仓数量（两腿）').textContent = current.variational_size + ' / ' + current.lighter_size;
                metricNodes.get('执行状态').textContent = current.partially_closed ? '部分已平仓 · 待核对' : current.status === 'reconcile_required' ? '待核对 · 持仓复核受阻' : openMonitorStatus(current);
                historyNote.textContent = current.mark_error || (current.mark_updated_at ? '持仓报价更新：' + new Date(current.mark_updated_at).toLocaleTimeString('zh-CN', {hour12:false}) : reason);
                if (last && !Number.isFinite(current.unrealized_pnl_usdc)) historyNote.textContent += ' · 最后有效估值：' + new Date(last.updated_at).toLocaleTimeString('zh-CN',{hour12:false});
                valuationNote.textContent = valuationText(current);
            };
        }
        const wear = document.createElement('p');
        wear.className = 'chart-note drawer-wear';
        wear.textContent = '磨损明细：' + wearDetail(row) + '。首档容量估算：' + amountU(row.max_notional_usdc) + '（执行时按数量与深度复核）' + (row.depth_reason ? '。' + row.depth_reason : '');
        const returnDetail = document.createElement('p'); returnDetail.className = 'chart-note drawer-wear drawer-return-detail';
        returnDetail.textContent = reversionEstimateDetail(row);
        const calculation = document.createElement('details'); calculation.className = 'desk-disclosure drawer-calculation';
        const calculationTitle = document.createElement('summary'); calculationTitle.textContent = '收益与成本明细';
        calculation.appendChild(calculationTitle); calculation.appendChild(returnDetail); calculation.appendChild(wear); body.appendChild(calculation);
        if (history && !historyClosed) calculation.appendChild(valuationNote);
        const reversion = document.createElement('p');
        reversion.className = 'chart-note drawer-wear';
        reversion.textContent = '正在读取近 7 天回归统计…';
        const reversionContainer = document.createElement('details'); reversionContainer.className='desk-disclosure';
        const label = document.createElement('summary'); label.textContent='历史回归统计';
        reversionContainer.appendChild(label); body.appendChild(reversionContainer);
        reversionContainer.appendChild(reversion);
        const reversionChart = document.createElement('div');
        reversionChart.className = 'drawer-reversion-chart';
        reversionChart.textContent = '正在读取历史价差曲线…';
        body.appendChild(reversionChart);
        if (history) {
            const note = document.createElement('p'); note.className = 'chart-note drawer-target-note';
            note.textContent = targetNote;
            body.appendChild(note);
            if (!historyClosed) metricNodes.get('平仓净收益估算').title = exitMetrics(history).detail;
        }
        const reversionDetails = document.createElement('div');
        reversionDetails.className = 'drawer-reversion-details';
        reversionContainer.appendChild(reversionDetails);
        const requestId = ++drawerRequestId;
        let quoteRevision = 0, lastStreamQuoteAt = 0, drawerQuoteRow = row;
        let liveHistory = [], liveStats = channelSpec.enabled ? {} : null, liveReference = [], lastHistoryRefreshAt = Date.now(), lastChartPaintAt = 0;
        let liveChannel = drawerChannelState(row, channelSpec);
        let liveCurrent = !history && !freshness.describe(row).stale && chartNumber(row.gross_bps) ? { timestamp: Date.now() / 1000, gross_bps: row.gross_bps } : null;
        if (liveChannel) liveCurrent = liveChannel.current == null ? null : {timestamp:Date.now()/1000,gross_bps:liveChannel.current};
        const paintChart = () => {
            if (requestId !== drawerRequestId || !liveStats) return;
            liveChannel = drawerChannelState(drawerQuoteRow, channelSpec);
            if (liveChannel) liveCurrent = liveChannel.current == null ? null : {timestamp:Date.now()/1000,gross_bps:liveChannel.current};
            else if (freshness.describe(drawerQuoteRow).stale) liveCurrent = null;
            renderReversionChart(reversionChart, liveHistory, liveStats, liveCurrent, liveReference, history?.entry_target?.target_bps, liveChannel);
            lastChartPaintAt = Date.now();
        };
        const reversionUrl = API + '/reversion-stats?symbol=' + encodeURIComponent(row.canonical_symbol || '') +
              '&pair=' + encodeURIComponent((channelSpec.canonical && !channelSpec.enabled ? channelSpec.axis : [row.buy_venue,row.sell_venue]).join('->')) +
              '&seconds=604800&band_bps=2&min_deviation_bps=' + encodeURIComponent(state.reversionMinDeviationBps) + '&target_mode=' + encodeURIComponent(state.reversionTargetMode);
        const historyUrl = API + '/history?symbol=' + encodeURIComponent(row.canonical_symbol || '') +
              '&pair=' + encodeURIComponent(channelSpec.axis.join('->')) + '&seconds=604800';
        const referenceUrl = API + '/reference-history?symbol=' + encodeURIComponent(row.canonical_symbol || '') +
              '&buy_venue=' + encodeURIComponent(channelSpec.axis[0]) + '&sell_venue=' + encodeURIComponent(channelSpec.axis[1]) +
              '&seconds=604800&interval_seconds=300';
        const fetchJson = url => fetch(url, {cache: 'no-store'}).then(response => { if (!response.ok) throw new Error('HTTP ' + response.status); return response.json(); });
        Promise.all([fetchJson(reversionUrl), fetchJson(historyUrl), fetchJson(referenceUrl).catch(() => ({points: []}))])
            .then(([stats, observations, reference]) => {
                if (requestId !== drawerRequestId) return;
                liveHistory = observations;
                liveStats = stats;
                liveReference = Array.isArray(reference?.points) ? reference.points : [];
                lastHistoryRefreshAt = Date.now();
                paintChart();
                const targetLabel = stats.target_mode === 'zero' ? '0' : stats.target_mode === 'either' ? '历史中心或 0' : '历史中心 ' + bpsPercent(stats.center_bps);
                const deviationLabel = Number(stats.min_deviation_bps || 0) > 0 ? '，最大偏离≥' + bpsPercent(stats.min_deviation_bps) : '';
                const anomalyLabel = Number(stats.anomaly_count || 0) > 0 ? '，已排除异常点 ' + stats.anomaly_count + ' 个' : '';
                const qualityLabel = '；有效样本覆盖 ' + elapsedLabel(stats.sample_span_seconds)
                    + '，最近有效样本 ' + (stats.last_sample_at == null ? '未知' : elapsedLabel(Math.max(0, Date.now()/1000-stats.last_sample_at)) + '前')
                    + (stats.invalid_quote_samples ? '，排除过期或缺失报价 ' + stats.invalid_quote_samples + ' 个' : '')
                    + (stats.center_ready === false ? '；中心尚未就绪，暂不可用于新开仓' : '');
                const prefix = (channelSpec.enabled ? '辅助统计（原开仓方向近7天，不用于绘制通道边界；目标 ' : '回归统计（近7天，目标 ') + targetLabel + ' ±' + Number(stats.band_bps || 2).toFixed(1) + ' bps' + deviationLabel + '）：';
                if (!stats.reversion_count) {
                    reversion.textContent = prefix + '暂未观察到完整回归；样本 ' + (stats.samples || 0) + ' 个' + anomalyLabel + qualityLabel + '。';
                    renderReversionDetails(reversionDetails, stats);
                    return;
                }
                reversion.textContent = prefix + '最近一次距今 ' + elapsedLabel(stats.last_reversion_age_seconds) +
                    '；典型间隔 ' + elapsedLabel(stats.median_interval_seconds) +
                    '；共 ' + stats.reversion_count + ' 次；样本 ' + stats.samples + ' 个' + anomalyLabel + qualityLabel + '。';
                renderReversionDetails(reversionDetails, stats);
            })
            .catch(() => { if (requestId === drawerRequestId) {
                reversion.textContent = '回归统计暂不可用；不会影响行情和执行前检查。'; reversionDetails.textContent = '';
                if (channelSpec.enabled) paintChart();
                else { disposeReversionChart(); reversionChart.replaceChildren(); reversionChart.textContent = '历史价差曲线暂不可用。'; }
            } });
        const refreshCurrentSpread = async () => {
            try {
                if (requestId !== drawerRequestId) return;
                updateQuoteMetrics(refreshReversionEstimate(drawerQuoteRow));
                // Expire the marker before waiting for any fallback network
                // request, so an old touch cannot remain labelled current.
                paintChart();
                // Reuse the scanner stream. A separate quote refresh cannot
                // carry forward a cost estimate calculated at older prices.
                if (!lastStreamQuoteAt || Date.now() - lastStreamQuoteAt > 5000) {
                    const revision = quoteRevision;
                    const symbol = encodeURIComponent((row.canonical_symbol || '').replace(/-PERP$/, ''));
                    const [buyResponse, sellResponse] = await Promise.all([
                        fetch(API + '/quotes?symbol=' + symbol + '&venue=' + encodeURIComponent(row.buy_venue), { cache: 'no-store' }),
                        fetch(API + '/quotes?symbol=' + symbol + '&venue=' + encodeURIComponent(row.sell_venue), { cache: 'no-store' }),
                    ]);
                    if (!buyResponse.ok || !sellResponse.ok) return;
                    const buyRows = await buyResponse.json(), sellRows = await sellResponse.json();
                    const buy = buyRows?.[0], sell = sellRows?.[0];
                    if (buy?.stale || sell?.stale) return;
                    const ask = Number(buy?.ask), bid = Number(sell?.bid);
                    if (!Number.isFinite(ask) || !Number.isFinite(bid) || ask <= 0 || bid <= 0) return;
                    if (requestId !== drawerRequestId) return;
                    if (revision === quoteRevision) { updateQuoteMetrics({...row,
                        buy_price:ask, sell_price:bid, gross_bps:(bid / ask - 1) * 10000,
                        buy_age_seconds:buy.age_seconds, sell_age_seconds:sell.age_seconds,
                        buy_source:buy.source, sell_source:sell.source, buy_stale:false, sell_stale:false,
                        _receivedAt:Date.now(), _deliverySeconds:0, net_bps:null, return_inputs:{buy,sell},
                        max_notional_usdc:null, wear_bps:null, wear_summary:null, wear_breakdown:{},
                        estimated_reversion_profit_usdc:null,
                        return_preview:{requested_target_mode:state.reversionTargetMode, window_seconds:state.reversionWindow,
                            net_bps:null, profit_usdc:null, meets_floor:false,
                            reason:'独立报价已更新，等待机会列表同轮整轮估算'}}); paintChart(); }
                }
                if (requestId === drawerRequestId && liveStats && Date.now() - lastHistoryRefreshAt >= 30000) {
                    const [statsResponse, historyResponse, referenceResponse] = await Promise.all([fetch(reversionUrl, { cache: 'no-store' }), fetch(historyUrl, { cache: 'no-store' }), fetch(referenceUrl, { cache: 'no-store' })]);
                    if (statsResponse.ok && historyResponse.ok) {
                        liveStats = await statsResponse.json();
                        liveHistory = await historyResponse.json();
                        liveReference = referenceResponse.ok ? ((await referenceResponse.json()).points || []) : [];
                        lastHistoryRefreshAt = Date.now();
                        paintChart();
                    }
                }
            } catch (_) { /* keep the last chart point while the drawer is open */ }
        };
        const status = document.createElement('div');
        status.className = 'drawer-status ' + (history ? (historyClosed ? 'positive' : 'warn') : (row.return_preview?.meets_floor ? 'positive' : 'warn'));
        status.textContent = history
            ? (historyClosed ? '该次执行已完成平仓；当前价差仅用于复盘，不代表新的机会。' : history.partially_closed ? '该次执行已部分平仓；当前价差仅为原路线行情参考，剩余仓位状态需核对。' : history.status === 'reconcile_required' ? '该次执行有待核对持仓；当前价差仅作参考，请先核对账户。' : '该次执行仍有未平仓双腿；当前展示实时价差和估算浮盈亏。')
            : row.direction_filter_reason || row.return_preview?.reason || '整轮收益估算待补；不能单凭毛价差或开仓净差判断入场';
        body.appendChild(status);
        function updateQuoteMetrics(current) {
            drawerQuoteRow = current;
            const p = current.return_preview || {};
            const values = {
                '买入 Ask':current.buy_price == null ? '--' : Number(current.buy_price).toLocaleString('en-US', {maximumFractionDigits:8}),
                '卖出 Bid':current.sell_price == null ? '--' : Number(current.sell_price).toLocaleString('en-US', {maximumFractionDigits:8}),
                '毛价差':bpsPercent(executionGross(current)), '开仓净差（未计平仓）':bpsPercent(current.net_bps),
                '整轮净价差':bpsPercent(p.net_bps), '预估回归净收益':reversionEstimate(current),
                '整轮入场门槛（含预留）':bpsPercent(p.minimum_bps),
                '首档容量估算（两腿等额）':amountU(current.max_notional_usdc), '磨损':wearDetail(current),
                '买入报价':freshness.describe(current).buyLabel, '卖出报价':freshness.describe(current).sellLabel
            };
            Object.entries(values).forEach(([label, value]) => { metricNodes.get(label).textContent = value; });
            metricNodes.get('整轮净价差').title = reversionEstimateDetail(current);
            returnDetail.textContent = reversionEstimateDetail(current);
            wear.textContent = '磨损明细：' + wearDetail(current) + '。首档容量估算：' + amountU(current.max_notional_usdc) + '（数量、最低开仓限制和实际深度仍需核验）';
            if (!history) {
                status.textContent = current.direction_filter_reason || p.reason || '等待同轮整轮收益估算；不能单凭毛价差判断入场';
                status.className = 'drawer-status ' + (p.meets_floor && !current.direction_filter_reason ? 'positive' : 'warn');
            }
            liveCurrent = !freshness.describe(current).stale && current.gross_bps != null
                ? {timestamp:Date.now()/1000, gross_bps:Number(current.gross_bps)} : null;
            liveChannel = drawerChannelState(current, channelSpec);
            if (liveChannel) liveCurrent = liveChannel.current == null ? null : {timestamp:Date.now()/1000,gross_bps:liveChannel.current};
        }
        drawerQuoteUpdate = rows => {
            let current = rows.find(item => item.canonical_symbol === row.canonical_symbol &&
                item.buy_venue === row.buy_venue && item.sell_venue === row.sell_venue);
            if (!current || requestId !== drawerRequestId) return;
            const directions={};
            rows.filter(item => item.canonical_symbol===row.canonical_symbol && [item.buy_venue,item.sell_venue].sort().join('|')===titleAxis.join('|'))
                .forEach(item=>{ directions[item.buy_venue===titleAxis[0]?'forward':'reverse']=item; });
            current={...current,pair_directions:directions};
            ++quoteRevision; lastStreamQuoteAt = Date.now();
            updateQuoteMetrics(current);
            // Reuse the open chart; limit stream repaints to once per second.
            if (Date.now() - lastChartPaintAt >= 1000) paintChart();
        };
        // Start only after all metric/status nodes have been initialized.
        paintChart();
        refreshCurrentSpread();
        drawerLiveTimer = setInterval(refreshCurrentSpread, 5000);
        const checks = document.createElement('ul'); checks.className = 'drawer-checks';
        ['买卖点差已包含在毛价差中，不重复计入磨损', '入场合计只含双腿开仓手续费和滑点，资金费与平仓成本另算', '两边报价必须在提交前重新获取', '手续费未知时不按零费用放行', '滑点或盘口深度过期会阻止执行', '成交回执未知时停止，不重复提交'].forEach(text => { const li = document.createElement('li'); li.textContent = text; checks.appendChild(li); });
        body.appendChild(checks);
        drawer.classList.add('is-open'); drawer.setAttribute('aria-hidden', 'false');
    }
    function closeDrawer() { drawerHistoryUpdate = null; drawerQuoteUpdate = null; ++drawerRequestId; const drawer = $('opportunity-drawer'); if (drawerLiveTimer) { clearInterval(drawerLiveTimer); drawerLiveTimer = null; } disposeReversionChart(); if (drawer) { drawer.classList.remove('is-open'); drawer.setAttribute('aria-hidden', 'true'); } }
    function visibleRows(rows) {
        const selected = state.selectedVenues;
        const excluded = new Set(state.excludedSymbols);
        return (rows || []).filter(row => selected.includes(row.buy_venue) && selected.includes(row.sell_venue) &&
            !excluded.has(symbolBase(row.canonical_symbol)));
    }
    function bestDirections(rows) {
        const pairs = new Map();
        (rows || []).forEach(row => {
            const axis = [row.buy_venue, row.sell_venue].sort();
            const key = (row.canonical_symbol || '') + '|' + axis.join('|');
            if (!pairs.has(key)) pairs.set(key, {axis, directions:{}});
            const group = pairs.get(key), side = row.buy_venue === axis[0] ? 'forward' : 'reverse';
            const previous = group.directions[side];
            if (!previous || compareOpportunity(row, previous) < 0) group.directions[side] = row;
        });
        return [...pairs.values()].flatMap(group => {
            const eligible = Object.values(group.directions).filter(row => !row.direction_filter_reason).sort(compareOpportunity);
            return eligible.length ? [{...eligible[0], pair_axis:group.axis, pair_directions:group.directions}] : [];
        }).sort(compareOpportunity);
    }
    const netBps = row => chartNumber(row?.return_preview?.net_bps) ? row.return_preview.net_bps.toFixed(2)+' bps' : '—';
    const directionClass = (side,row) => side+'-net '+(chartNumber(row?.return_preview?.net_bps) ? row.return_preview.net_bps>=0?'positive':'negative' : 'muted');
    function directionDetail(row) {
        return row ? (row.direction_filter_reason ? row.direction_filter_reason+'\n' : '') + reversionEstimateDetail(row) : '此方向尚无新鲜双边报价；不会使用另一方向的相反数代替。';
    }
    function opportunityStatus(preview) {
        if (preview.meets_floor) return '收益达标 · 待实盘复核';
        if (Number.isFinite(preview.net_bps) && Number.isFinite(preview.minimum_bps) && preview.net_bps < preview.minimum_bps)
            return '净收益不足 · 差 '+(preview.minimum_bps-preview.net_bps).toFixed(2)+' bps';
        return preview.reason || '整轮成本待核验';
    }
    function renderResult(rows, error) {
        const root = $('v2-opportunity-result');
        root.replaceChildren();
        const head = document.querySelector?.('#cross-venue-scanner .opportunity-table-head');
        if (head && !head.dataset.paired) {
            ['品种','固定平台 A / B','优选买入 / 卖出','A买 / B卖 · 整轮净差','B买 / A卖 · 整轮净差','优选方向 / 预计净收益','首档可见容量（估算）','磨损','历史价差（24h）','报价时效','执行状态'].forEach((label,i) => { if (head.children[i]) head.children[i].textContent=label; });
            head.dataset.paired='1';
        }
        // The live websocket deliberately omits the slower historical
        // aggregate. Keep the last HTTP-provided range attached as quotes
        // refresh so the diagnostic column does not flicker away.
        rows = rows.map(row => {
            const key = (row.canonical_symbol || '') + '|' + row.buy_venue + '->' + row.sell_venue;
            if (Number(row.history_samples || 0) > 0 || Number(row.history_7d_samples || 0) > 0) state.historyStats.set(key, {
                history_samples: row.history_samples,
                history_min_bps: row.history_min_bps,
                history_max_bps: row.history_max_bps,
                history_avg_bps: row.history_avg_bps,
                history_7d_samples: row.history_7d_samples,
                history_7d_min_bps: row.history_7d_min_bps,
                history_7d_max_bps: row.history_7d_max_bps,
                history_7d_avg_bps: row.history_7d_avg_bps,
                history_center_bps: row.history_center_bps,
                history_p10_bps: row.history_p10_bps,
                history_p90_bps: row.history_p90_bps,
                history_reversion_count: row.history_reversion_count,
                history_last_reversion_age_seconds: row.history_last_reversion_age_seconds,
                history_median_interval_seconds: row.history_median_interval_seconds,
            });
            const previous = state.historyStats.get(key);
            return refreshReversionEstimate(previous && !Number(row.history_samples || 0) && !Number(row.history_7d_samples || 0) ? {...row, ...previous} : row);
        });
        // Keep the opened route current even if it no longer passes a filter.
        drawerQuoteUpdate?.(rows);
        // Restore diagnostics before filtering both routes and ranking the
        // surviving directions, consistently for REST and websocket updates.
        // A stale quote is useful for diagnostics/history, but it is not a
        // current opportunity. Keep it out of the main list so an old row
        // cannot look like a live candidate while a slow venue refreshes.
        const staleCount = rows.reduce((count, row) => count + (freshness.describe(row).stale ? 1 : 0), 0);
        state.hiddenStaleCount = staleCount;
        rows = rows.filter(row => !freshness.describe(row).stale);
        const reversionCandidates = rows.slice();
        rows = rows.map(row => ({...row, direction_filter_reason: !spreadFilterAllowed(row) ? '未通过毛价差筛选'
            : reversionFilterActive() && !reversionAllowed(row) ? '未通过历史回归筛选' : null}));
        rows = bestDirections(rows);
        state.rows = rows;
        const coverage = $('v2-scan-coverage');
        if (coverage) coverage.textContent = `扫描范围 ${state.liveSymbols.length} 个品种 · 已收到双边报价 ${new Set(state.allRows.map(row => row.canonical_symbol)).size} 个 · 当前新鲜 ${new Set(reversionCandidates.map(row => row.canonical_symbol)).size} 个 · 筛选后 ${rows.length} 条路线` + (state.reversionLoading ? ' · 历史统计刷新中' : '');
        if (reversionFilterActive()) ensureReversionStats(reversionCandidates);
        updateHistoryWindowLabel();
        updateHistoryRoutes(rows);
        if (error) { const node = document.createElement('span'); node.className = 'v2-unavailable'; node.textContent = '报价暂不可用 · ' + error; root.appendChild(node); return; }
        if (!rows.length) {
            const node = document.createElement('span'); node.className = error ? 'v2-unavailable' : 'muted';
            const diagnostics = state.lastScanFailures.length
                ? ' · ' + [...new Set(state.lastScanFailures)].slice(0, 2).join('；')
                : '';
            const venueIssues = state.selectedVenues.map(venue => {
                const item = state.venueDiagnostics[venue];
                return item && item.ok === false ? names(venue) + '：' + item.detail : '';
            }).filter(Boolean);
            const waiting = state.hiddenStaleCount
                ? `等待新鲜报价 · ${state.hiddenStaleCount} 条旧报价已隐藏`
                : reversionFilterActive() && state.reversionLoading ? '回归统计读取中，请稍候…'
                : reversionFilterActive() && reversionCandidates.length ? '当前新鲜报价未通过历史回归筛选' : '没有形成可计算的双边报价';
            node.textContent = (error || waiting) + (diagnostics || (venueIssues.length ? ' · ' + venueIssues.join('；') : ''));
            root.appendChild(node); return;
        }
        rows.forEach(row => {
            const card = document.createElement('div'); card.className = 'v2-opportunity-row'; card.setAttribute('role', 'row'); card.tabIndex = 0;
            const preview = row.return_preview || {};
            const hasReversionScore = preview.net_bps != null && row.estimated_reversion_profit_usdc != null;
            const [a,b] = row.pair_axis, forward = row.pair_directions.forward, reverse = row.pair_directions.reverse;
            const values = [
                [(row.canonical_symbol || '--').replace(/-PERP$/, ''), 'symbol', '品种'],
                ['A '+venueShort(a) + ' / B ' + venueShort(b), 'route', '固定平台 A / B'],
                [Number(row.buy_price || 0).toLocaleString('en-US', { maximumFractionDigits: 6 }) + ' / ' + Number(row.sell_price || 0).toLocaleString('en-US', { maximumFractionDigits: 6 }), 'prices', '买入 / 卖出'],
                [netBps(forward), directionClass('forward',forward), 'A买 / B卖 · 整轮净差'],
                [netBps(reverse), directionClass('reverse',reverse), 'B买 / A卖 · 整轮净差'],
                [(row.buy_venue===a?'A买 / B卖':'B买 / A卖')+' · '+reversionEstimate(row), hasReversionScore ? 'reversion-score '+(preview.profit_usdc>=0?'positive':'negative') : 'reversion-score muted', '优选方向 / 预计净收益'],
                [amountU(row.max_notional_usdc), row.max_notional_usdc == null ? 'max muted' : 'max', '首档容量估算'],
                [wearLabel(row), 'wear', '磨损'],
                [historyRange(row), 'history-range', state.historyWindow === 604800 ? '历史价差（7天）' : '历史价差（24h）'],
                ['买 ' + age(row.buy_age_seconds) + ' · 卖 ' + age(row.sell_age_seconds), 'age', '报价时效'],
                [opportunityStatus(preview), preview.meets_floor ? 'status positive' : 'status warn', '状态']
            ];
            values.forEach(([text, className, label]) => {
                const cell = document.createElement('span'); cell.className = className;
                cell.textContent = text; cell.dataset.label = label==='整轮净价差'?'预计整轮净价差':label==='预估回归净收益'?'预计净收益':label; cell.setAttribute('role', 'cell');
                if (label === '磨损') {
                    cell.title = wearDetail(row); const detail = document.createElement('small');
                    detail.textContent = row.wear_bps != null ? '手续费 + 盘口冲击（估算）' : (row.wear_breakdown?.fees_bps != null ? '滑点待测 · 合计未齐' : '费用待核验 · 合计未齐'); cell.appendChild(detail);
                }
                if (className.startsWith('forward-net') || className.startsWith('reverse-net')) {
                    const direction = className.startsWith('forward-net') ? forward : reverse;
                    cell.title = directionDetail(direction);
                    if (direction) {
                        const show = () => openDrawer({...direction,pair_axis:row.pair_axis,pair_directions:row.pair_directions});
                        cell.tabIndex=0;
                        cell.addEventListener('click', event => { event.stopPropagation(); show(); });
                        cell.addEventListener('keydown', event => { if (event.key==='Enter' || event.key===' ') {event.preventDefault();event.stopPropagation();show();} });
                    }
                }
                if (label === '预估回归净收益' || label === '状态') cell.title = (preview.reason || '估算待核验')+'\n'+reversionEstimateDetail(row).split('\n').slice(0,2).join('\n')+'\n点击查看计算明细。';
                if (label === '历史价差（24h）' || label === '历史价差（7天）') cell.title = historyRangeDetail(row);
                if (label === '首档容量估算') cell.title = row.max_base_amount == null ? (row.depth_reason || '任一侧未提供首档数量') : '两边首档可成交数量取较小值：'+Number(row.max_base_amount).toLocaleString('en-US',{maximumFractionDigits:8})+' base；两腿按相同基础币数量成交';
                card.appendChild(cell);
            });
            card.quoteRow = row;
            card.addEventListener('click', () => openDrawer(row)); card.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openDrawer(row); } });
            root.appendChild(card);
        });
        updateFreshness();
    }
    function updateFreshness() {
        const now = Date.now();
        recoverOpportunityStream(now);
        recoverVenueHealth(now);
        updateVenuePickerHealth();
        let expired = 0;
        document.querySelectorAll('#v2-opportunity-result .v2-opportunity-row').forEach(card => {
            const row = card.quoteRow;
            if (!row) return;
            const data = freshness.describe(row, now);
            if (data.stale) {
                // Remove a row as soon as its 10-second freshness window ends;
                // the next stream/scan can add it back after a fresh quote.
                card.remove();
                state.hiddenStaleCount = (state.hiddenStaleCount || 0) + 1;
                return;
            }
            const cell = card.querySelector('.age');
            if (cell) { cell.textContent = data.label; cell.title = data.detail; }
            for (const [side, selector] of [['forward','.forward-net'],['reverse','.reverse-net']]) {
                const node=card.querySelector(selector), direction=row.pair_directions?.[side];
                if (node) { const current=direction ? refreshReversionEstimate(direction) : null; node.textContent=netBps(current); node.title=directionDetail(current); node.className=directionClass(side,current); }
            }
            card.classList.toggle('quote-expired', data.stale);
            const status = card.querySelector('.status');
            if (status) {
                const current = refreshReversionEstimate(row);
                const p = current.return_preview || {};
                if (row.return_preview?.net_bps != null && p.net_bps == null) {
                    card.quoteRow = current;
                    const net = card.querySelector('.net'), profit = card.querySelector('.reversion-score');
                    if (net) { net.textContent = '—'; net.className = 'net muted'; net.title = p.reason; }
                    if (profit) { profit.textContent = '—'; profit.title = p.reason; }
                }
                status.textContent = opportunityStatus(p);
                status.title = (p.reason || '估算待核验')+'\n'+reversionEstimateDetail(current).split('\n').slice(0,2).join('\n')+'\n点击查看计算明细。';
                status.className = 'status ' + (p.meets_floor ? 'positive' : 'warn');
            }
            if (data.stale) expired += 1;
        });
        const node = $('v2-update-info');
        if (node) {
            const mode = state.loading ? '正在读取行情' : state.liveConnected ? '行情长连接推送' : (state.auto ? '每 ' + refreshInterval() / 1000 + ' 秒扫描' : '自动更新已暂停');
            const loadingAge = state.scanStartedAt == null ? '' : ' · 本轮已用 ' + ((now - state.scanStartedAt) / 1000).toFixed(1) + ' 秒';
            const timing = state.lastResponseAt == null ? ' · 等待报价' : ' · 页面收到数据 ' + ((now - state.lastResponseAt) / 1000).toFixed(1) + ' 秒前' + (state.liveConnected ? '' : ' · 上批耗时 ' + (state.lastBatchMs / 1000).toFixed(2) + ' 秒') + loadingAge;
            const hidden = state.hiddenStaleCount ? ' · ' + state.hiddenStaleCount + ' 条旧报价已隐藏' : '';
            node.textContent = mode + timing + hidden + (expired ? ' · ' + expired + ' 条报价已过期' : '');
        }
    }
    // The execution panel may have more than two scan venues selected.  A
    // signed two-leg order still needs one concrete pair; expose the best
    // fresh opportunity so the execution check can bind to that pair.  The v2
    // scanner deliberately labels every quote as read-only (it must never
    // submit an order), so `row.executable` cannot be used as the execution
    // gate here.  The legacy execution check performs the credential, fee,
    // account and final quote validation before it can start a worker.
    const executionVenues = new Set(['variational', 'lighter', 'lighter_rh', 'standx', 'arcus']);
    const signedExecutionVenues = new Set(['lighter', 'lighter_rh', 'standx', 'arcus']);
    const executablePair = (buy, sell) =>
        (signedExecutionVenues.has(buy) && signedExecutionVenues.has(sell)) ||
        ((buy === 'variational' && sell === 'lighter') || (buy === 'lighter' && sell === 'variational'));
    window.getBestExecutionVenues = (options = {}) => {
        const fresh = (state.rows || []).filter(row =>
            state.selectedVenues.includes(row.buy_venue) &&
            state.selectedVenues.includes(row.sell_venue) &&
            row.buy_venue !== row.sell_venue &&
            executionVenues.has(row.buy_venue) && executionVenues.has(row.sell_venue) &&
            executablePair(row.buy_venue, row.sell_venue) &&
            !row.buy_stale && !row.sell_stale);
        // For an actual submission, prefer a known positive net edge. During
        // read-only preparation a fresh best route may still be used to bind
        // the two adapters; the worker re-quotes and re-checks profitability
        // before any order is submitted.
        const requirePositive = options.requirePositive !== false;
        const pool = requirePositive ? fresh.filter(row => row.return_preview?.net_bps != null
            ? Number(row.return_preview.net_bps) > 0
            : !row.return_preview?.version && row.net_bps != null && Number(row.net_bps) > 0) : fresh;
        pool.sort(compareOpportunity);
        const row = pool[0];
        return row ? [row.buy_venue, row.sell_venue] : null;
    };
    // Used by the execution panel to show a symbol-specific minimum-order
    // estimate. This is read-only context; the execution worker still
    // re-quotes and validates the pair immediately before submitting.
    window.getBestExecutionOpportunity = () => {
        const fresh = (state.rows || []).filter(row =>
            state.selectedVenues.includes(row.buy_venue) &&
            state.selectedVenues.includes(row.sell_venue) &&
            row.buy_venue !== row.sell_venue &&
            executionVenues.has(row.buy_venue) && executionVenues.has(row.sell_venue) &&
            !row.buy_stale && !row.sell_stale);
        const pool = fresh.filter(row => row.net_bps != null &&
            Number.isFinite(Number(row.net_bps)) && Number(row.net_bps) > 0);
        pool.sort(compareOpportunity);
        return pool[0] || null;
    };
    function closeOpportunityStream(permanent = true) {
        if (permanent) {
            state.liveStreamWanted = false;
            state.liveReconnectAttempt = 0;
            if (state.liveReconnectTimer) { clearTimeout(state.liveReconnectTimer); state.liveReconnectTimer = null; }
        }
        const socket = state.liveSocket;
        state.liveSocket = null;
        state.liveConnected = false;
        state.liveSymbolsKey = '';
        if (socket) socket.close();
        updateFreshness();
    }
    function recoverOpportunityStream(now = Date.now()) {
        const socket = state.liveSocket;
        if (!state.liveStreamWanted) return;
        if (!socket) { scheduleOpportunityReconnect(); return; }
        const last = state.liveLastMessageAt ?? state.liveStartedAt;
        const timeout = socket.readyState === 0 ? 20000 : 30000;
        if (!Number.isFinite(last) || now-last <= timeout) return;
        // Detach first so late close/message callbacks cannot disturb the
        // replacement. A heartbeat renews transport health, never quote age.
        state.liveSocket = null;
        state.liveConnected = false;
        state.liveSymbolsKey = '';
        socket.close();
        scheduleOpportunityReconnect();
    }
    function scheduleOpportunityReconnect() {
        if (!state.liveStreamWanted || state.liveReconnectTimer || state.liveSymbols.length === 0 || state.selectedVenues.length < 2) return;
        const delay = Math.min(5000, 1000 * Math.max(1, state.liveReconnectAttempt + 1));
        state.liveReconnectAttempt = Math.min(state.liveReconnectAttempt + 1, 5);
        setApiState('行情长连接断开 · ' + Math.ceil(delay / 1000) + ' 秒后自动重连', 'warn');
        state.liveReconnectTimer = setTimeout(() => {
            state.liveReconnectTimer = null;
            if (state.liveStreamWanted) startOpportunityStream();
        }, delay);
    }
    function startOpportunityStream() {
        // Keep the stream alive even when the last REST scan returned no
        // opportunity rows.  The old implementation derived symbols from
        // allRows, so an empty scan disabled the only real-time recovery path
        // and the page silently fell back to a 30-second poll.
        // Pass the complete common catalog. The API limits work per rolling
        // window, so the URL carries the catalog while one slow batch cannot
        // block the rest of the live stream.
        const symbols = [...new Set((state.liveSymbols.length ? state.liveSymbols : state.allRows.map(row => row.canonical_symbol)).filter(Boolean))];
        const venues = catalogVenues();
        if (symbols.length === 0 || venues.length < 2) { closeOpportunityStream(); return; }
        const notional = Number($('notional')?.value || 1000);
        const key = venues.join(',') + '|' + symbols.join(',') + '|' + notional + '|' + spreadQuery();
        if (state.liveSocket && state.liveSymbolsKey === key && state.liveSocket.readyState < 2) return;
        state.liveStreamWanted = true;
        if (state.liveReconnectTimer) { clearTimeout(state.liveReconnectTimer); state.liveReconnectTimer = null; }
        const oldSocket = state.liveSocket;
        state.liveSocket = null;
        state.liveConnected = false;
        state.liveSymbolsKey = '';
        if (oldSocket) oldSocket.close();
        const wsBase = (location.protocol === 'https:' ? 'wss://' : 'ws://') + '127.0.0.1:5002/api/v2/ws/opportunities';
        const socket = new WebSocket(wsBase + '?symbols=' + encodeURIComponent(symbols.join(',')) + '&venues=' + encodeURIComponent(venues.join(',')) + '&notional=' + encodeURIComponent(notional) + spreadQuery());
        state.liveSocket = socket; state.liveSymbolsKey = key;
        state.liveStartedAt = Date.now(); state.liveLastMessageAt = null;
        socket.onopen = () => { if (state.liveSocket === socket) { state.liveConnected = true; state.liveLastMessageAt = Date.now(); state.liveReconnectAttempt = 0; setApiState('行情长连接已接通', 'positive'); updateFreshness(); } };
        socket.onmessage = event => {
            if (state.liveSocket !== socket) return;
            try {
                const data = JSON.parse(event.data);
                if (!Array.isArray(data.rows)) return;
                const received = Date.now();
                state.liveLastMessageAt = received;
                if (data.heartbeat) {
                    setApiState('行情长连接已接通 · 正在刷新盘口', 'neutral');
                    updateFreshness();
                    return;
                }
                if (!data.rows.length && !Array.isArray(data.window_symbols)) {
                    // A transient empty calculation must not make the table
                    // flicker away. Keep the last valid rows and let their
                    // age indicator expose the stale feed instead.
                    setApiState('行情长连接已接通 · 等待新盘口', 'warn');
                    updateFreshness();
                    return;
                }
                data.rows.forEach(row => freshness.stamp(row, received));
                updateVenueDiagnosticsFromRows(data.rows, received);
                state.lastResponseAt = received;
                if (Array.isArray(data.window_symbols)) {
                    const windowSet = new Set(data.window_symbols.map(symbolBase));
                    const retained = state.allRows.filter(row => !windowSet.has(symbolBase(row.canonical_symbol)));
                    state.allRows = retained.concat(data.rows);
                    const total = Number(data.window_total || state.liveSymbols.length || 0);
                    const offset = Number(data.window_offset || 0);
                    const windowLabel = total > 100 ? ` · 滚动 ${Math.min(offset + 100, total)}/${total}` : '';
                    setApiState('行情长连接 · ' + data.rows.length + ' 条机会' + windowLabel, 'positive');
                } else {
                    state.allRows = data.rows.slice();
                    setApiState('行情长连接 · ' + data.rows.length + ' 条机会', 'positive');
                }
                renderResult(visibleRows(state.allRows));
            } catch (_) { /* ignore one malformed push and await the next */ }
        };
        socket.onerror = () => { if (state.liveSocket === socket) socket.close(); };
        socket.onclose = () => { if (state.liveSocket === socket) { state.liveSocket = null; state.liveConnected = false; updateFreshness(); scheduleOpportunityReconnect(); } };
    }
    function renderIdle(message) {
        const root = $('v2-opportunity-result');
        if (!root) return;
        root.replaceChildren();
        const node = document.createElement('span');
        node.className = 'muted';
        node.textContent = message;
        root.appendChild(node);
        updateHistoryRoutes([]);
    }
    function updateHistoryRoutes(rows) {
        const select = $('history-pair');
        if (!select) return;
        const routes = [...new Map((rows || []).map(row => {
            const axis=row.pair_axis || [row.buy_venue,row.sell_venue].sort();
            return [axis.join('->'),{buy_venue:axis[0],sell_venue:axis[1]}];
        })).values()];
        const current = activeHistoryRoute();
        select.replaceChildren();
        const all = document.createElement('option'); all.value = ''; all.textContent = '全部已选路线'; select.appendChild(all);
        routes.forEach(row => {
            const option = document.createElement('option'); option.value = row.buy_venue + '->' + row.sell_venue;
            option.textContent = venueShort(row.buy_venue) + ' → ' + venueShort(row.sell_venue); select.appendChild(option);
        });
        select.value = current && routes.some(row => row.buy_venue + '->' + row.sell_venue === current) ? current : '';
    }
    function activeHistoryRoute() {
        const select = $('history-pair');
        return select?.value || '';
    }
    function commonSymbols(venues) {
        const counts = new Map();
        new Set(venues).forEach(venue => {
            const symbols = new Set((state.instruments[venue] || []).filter(row => row.enabled !== false)
                .map(row => symbolBase(row.canonical_symbol || row.venue_symbol)).filter(Boolean));
            symbols.forEach(symbol => counts.set(symbol, (counts.get(symbol) || 0) + 1));
        });
        const excluded = new Set(state.excludedSymbols.map(symbolBase));
        return [...counts].filter(([symbol, count]) => count >= 2 && !excluded.has(symbol))
            .map(([symbol]) => symbol).sort();
    }
    function batches(values, size = 8) {
        const result = [];
        for (let index = 0; index < values.length; index += size) result.push(values.slice(index, index + size));
        return result;
    }
    async function scan(forceCatalog = true) {
        state.scanController?.abort();
        const controller = new AbortController();
        state.scanController = controller;
        state.loading = true;
        state.scanStartedAt = Date.now();
        state.lastScanFailures = [];
        const selected = selectedVenues();
        state.selectedVenues = selected;
        if (selected.length < 2) { state.loading = false; state.scanStartedAt = null; closeOpportunityStream(); renderIdle(selected.length ? '再选择一个平台后开始扫描' : '选择至少两个平台后开始扫描'); setApiState('等待选择', 'neutral'); return; }
        if (selected.some(venue => !(state.instruments[venue] || []).length)) {
            // Recover missing catalogs without delaying healthy pairs. A manual
            // scan can retry immediately; automatic scans respect backoff.
            const pending = refreshCatalog(forceCatalog);
            if (catalogVenues().length < 2) await pending;
            if (state.scanController !== controller || controller.signal.aborted) return;
        }
        const all = state.symbolMode === 'all';
        const symbols = all ? commonSymbols(selected) : state.selectedSymbols.slice();
        const availableVenues = catalogVenues();
        if (!symbols.length || availableVenues.length < 2) {
            state.loading = false; state.scanStartedAt = null;
            closeOpportunityStream();
            const warning = catalogWarning(selected);
            renderResult([], warning || (all ? '目录已加载，但任意两家之间没有可扫描的共同品种（含排除设置）' : '请至少选择一个指定品种'));
            setApiState(warning ? '部分平台目录不可用 · 自动重试中' : '等待品种', 'neutral');
            return;
        }
        const groups = batches(symbols);
        state.liveSymbols = symbols.slice();
        const rows = [];
        try {
            const venueQuery = '&venues=' + encodeURIComponent(availableVenues.join(','));
            const configuredNotional = Number($('notional')?.value || 1000);
            const notionalQuery = '&notional=' + encodeURIComponent(Number.isFinite(configuredNotional) && configuredNotional >= 1 ? configuredNotional : 1000);
            setApiState(`并行读取 ${groups.length} 批 · 共 ${symbols.length} 个品种`, 'neutral');
            // Keep the existing push channel alive while REST batches are in
            // flight.  It continues to update the table during a slow scan.
            try { startOpportunityStream(); } catch (_) { /* WS is best-effort */ }
            let done = 0, failed = 0;
            const CONCURRENCY = 2;
            const queue = groups.slice();
            // A cold Lighter/RH REST fallback can take longer than a normal
            // quote refresh while its native websocket cache is warming.
            // Allow the first scan to finish instead of retaining old rows.
            const watchdog = setTimeout(() => controller.abort(), 90000);
            const workers = Array.from({ length: Math.min(CONCURRENCY, queue.length) }, async () => {
                while (queue.length) {
                    if (controller.signal.aborted || state.scanController !== controller) return;
                    const group = queue.shift();
                    const path = '/opportunities?symbols=' + encodeURIComponent(group.join(',')) + venueQuery + notionalQuery + spreadQuery();
                    const batchStarted = performance.now();
                    try {
                        // Do not discard a batch at 12s just because a cold
                        // public order-book request is still warming.
                        const data = await apiFetch(path, 30000, controller.signal, true);
                        if (state.scanController !== controller) return;
                        const received = Date.now();
                        data.rows.forEach(row => freshness.stamp(row, received));
                        updateVenueDiagnosticsFromRows(data.rows, received);
                        state.lastResponseAt = received;
                        state.lastBatchMs = performance.now() - batchStarted;
                        rows.push(...data.rows);
                        state.allRows = rows.slice();
                        renderResult(visibleRows(rows));
                    } catch (error) {
                        failed += 1;
                        state.lastScanFailures.push(error?.name === 'AbortError' ? '行情请求超时' : (error?.message || '行情请求失败'));
                    }
                    done += 1;
                    if (state.scanController === controller) setApiState(`已处理 ${done}/${groups.length} 批 · ${rows.length} 条机会${failed ? ' · ' + failed + ' 批失败已跳过' : ''}`, 'neutral');
                }
            });
            await Promise.all(workers);
            clearTimeout(watchdog);
            if (state.scanController !== controller) return;
            setApiState('扫描完成 · ' + new Date().toLocaleTimeString('zh-CN', { hour12: false }), 'positive');
            if (!rows.length && state.lastScanFailures.length) {
                renderResult([], '没有收到可计算的双边报价');
            }
            startOpportunityStream();
        } catch (error) {
            if (state.scanController !== controller || controller.signal.aborted) return;
            renderResult([], error.name === 'AbortError' ? '扫描超时，可缩小品种范围后重试' : '行情接口暂不可用');
            setApiState(error.name === 'AbortError' ? '扫描超时' : '行情接口异常', 'negative');
        } finally {
            if (state.scanController === controller) { state.loading = false; state.scanController = null; state.scanStartedAt = null; if (state.liveStreamWanted && !state.liveSocket) startOpportunityStream(); updateFreshness(); }
        }
    }
    async function scanBest() {
        state.symbolMode = 'all';
        renderSymbolCatalog();
        updateHistoryVisibility();
        startAuto();
        await scan();
    }
    async function load() {
        try {
            const venues = await apiFetch('/venues');
            state.venues = venues;
            renderCatalog(); renderVenueState(); setApiState('请选择至少两个平台', 'neutral');
            const catalogReady = await refreshCatalog();
            if (state.selectedVenues.length >= 2 && catalogReady) {
                probeSelectedVenues(catalogVenues());
                await scan(false);
            } else if (state.selectedVenues.length >= 2) {
                await scan(false);
            }
        } catch (error) { setApiState('API 断开', 'negative'); renderResult([], error.name === 'AbortError' ? '目录请求超时' : '请启动 5002 API'); }
    }
    function refreshInterval() { return state.refreshMs || (state.symbolMode === 'all' ? 10000 : 5000); }
    function startAuto() {
        clearInterval(state.timer);
        const interval = refreshInterval();
        state.timer = state.auto ? setInterval(() => { if (!state.loading) scan(false); }, interval) : null;
        updateFreshness();
    }
    document.addEventListener('DOMContentLoaded', () => {
        $('v2-refresh-speed')?.addEventListener('change', event => {
            state.refreshMs = validRefreshMs(event.target.value);
            savePrefs();
            startAuto();
        });
        if ($('v2-refresh-speed')) $('v2-refresh-speed').value = String(state.refreshMs);
        $('v2-history-window')?.addEventListener('change', event => {
            state.historyWindow = Number(event.target.value) === 604800 ? 604800 : 86400;
            savePrefs();
            updateHistoryWindowLabel();
            renderResult(visibleRows(state.allRows));
        });
        if ($('v2-history-window')) $('v2-history-window').value = String(state.historyWindow);
        updateHistoryWindowLabel();
        syncSpreadFilterControls();
        syncReversionFilterControls();
        const applyReversionFilter = () => {
            const windowValue = Number($('v2-reversion-window')?.value || 604800);
            const minCount = Number($('v2-reversion-min-count')?.value || 0);
            const minDeviationPercent = Number($('v2-reversion-min-deviation')?.value || 0);
            const maxAgeHours = Number($('v2-reversion-max-age')?.value || 0);
            state.reversionRecencyMode = $('v2-reversion-recency-mode')?.value === 'soft' ? 'soft' : 'hard';
            const minSamples = Number($('v2-reversion-min-samples')?.value || 0);
            const targetMode = $('v2-reversion-target')?.value || 'center';
            const nextWindow = [86400, 259200, 604800].includes(windowValue) ? windowValue : 604800;
            const nextTargetMode = ['center', 'zero', 'either'].includes(targetMode) ? targetMode : 'center';
            const nextMinDeviationBps = Number.isFinite(minDeviationPercent) && minDeviationPercent >= 0 ? minDeviationPercent * 100 : 0;
            if (nextWindow !== state.reversionWindow || nextTargetMode !== state.reversionTargetMode || nextMinDeviationBps !== state.reversionMinDeviationBps) { state.reversionStats.clear(); state.reversionAttempts.clear(); }
            state.reversionWindow = nextWindow;
            state.reversionMinCount = Number.isFinite(minCount) && minCount >= 0 ? minCount : 0;
            state.reversionMinDeviationBps = nextMinDeviationBps;
            state.reversionMaxAge = Number.isFinite(maxAgeHours) && maxAgeHours >= 0 ? maxAgeHours * 3600 : 0;
            state.reversionMinSamples = Number.isFinite(minSamples) && minSamples >= 0 ? Math.floor(minSamples) : 0;
            state.reversionTargetMode = nextTargetMode;
            state.reversionRequestId += 1;
            state.reversionLoading = false;
            if (!reversionFilterActive()) state.reversionStats.clear();
            window.dispatchEvent(new CustomEvent('execution-selection-changed'));
            savePrefs();
            syncReversionFilterControls();
            startOpportunityStream();
            renderResult(visibleRows(state.allRows));
        };
        $('v2-reversion-window')?.addEventListener('change', applyReversionFilter);
        $('v2-reversion-min-count')?.addEventListener('change', applyReversionFilter);
        $('v2-reversion-min-deviation')?.addEventListener('change', applyReversionFilter);
        $('v2-reversion-max-age')?.addEventListener('change', applyReversionFilter);
        $('v2-reversion-recency-mode')?.addEventListener('change', applyReversionFilter);
        $('v2-reversion-min-samples')?.addEventListener('change', applyReversionFilter);
        $('v2-reversion-target')?.addEventListener('change', applyReversionFilter);
        const applySpreadFilter = () => {
            const minPercent = Number($('v2-spread-min')?.value || 0);
            const maxPercent = Number($('v2-spread-max')?.value || 0);
            state.spreadMinBps = Number.isFinite(minPercent) && minPercent >= 0 ? minPercent * 100 : 0;
            state.spreadMaxBps = Number.isFinite(maxPercent) && maxPercent > 0 ? maxPercent * 100 : 0;
            if (state.spreadMaxBps && state.spreadMaxBps < state.spreadMinBps) state.spreadMaxBps = state.spreadMinBps;
            window.dispatchEvent(new CustomEvent('execution-selection-changed'));
            savePrefs();
            syncSpreadFilterControls();
            renderResult(visibleRows(state.allRows));
            closeOpportunityStream(false);
            startOpportunityStream();
        };
        $('v2-spread-min')?.addEventListener('change', applySpreadFilter);
        $('v2-spread-max')?.addEventListener('change', applySpreadFilter);
        setInterval(updateFreshness, 1000);
        document.addEventListener('visibilitychange', updateFreshness);
        $('v2-venue-select')?.addEventListener('change', () => {
            closeOpportunityStream();
            state.selectedVenues = selectedVenues();
            window.dispatchEvent(new CustomEvent('execution-selection-changed'));
            savePrefs();
            window.setHistoryVenues?.(state.selectedVenues);
            document.querySelectorAll('.v2-status-card[data-venue]').forEach(card => card.classList.toggle('is-unselected', !state.selectedVenues.includes(card.dataset.venue)));
            const hint = $('v2-venue-hint');
            if (hint) hint.textContent = state.selectedVenues.length < 2
                ? '至少选择两个；当前已选 ' + state.selectedVenues.length + '/' + state.venues.length
                : '已选 ' + state.selectedVenues.length + '/' + state.venues.length + '；方向由系统自动比较';
            renderResult(visibleRows(state.allRows));
            refreshCatalog().then(() => {
                if (state.selectedVenues.length < 2) return;
                window.dispatchEvent(new CustomEvent('execution-platform-scope-ready'));
                probeSelectedVenues(catalogVenues());
                scan(false);
            });
        });
        $('v2-symbol-mode')?.addEventListener('change', event => {
            closeOpportunityStream();
            state.symbolMode = event.target.value === 'selected' ? 'selected' : 'all';
            window.dispatchEvent(new CustomEvent('execution-selection-changed'));
            savePrefs();
            renderSymbolCatalog();
            updateHistoryVisibility();
            startAuto();
            scan(false);
        });
        $('v2-symbol-select')?.addEventListener('change', () => {
            closeOpportunityStream();
            state.selectedSymbols = selectedSymbols();
            window.dispatchEvent(new CustomEvent('execution-selection-changed'));
            savePrefs();
            renderSymbolCatalog();
            updateHistoryVisibility();
            if (state.symbolMode === 'selected') scan(false);
        });
        $('v2-symbol-search')?.addEventListener('input', filterSymbolOptions);
        $('v2-exclude-search')?.addEventListener('input', filterExcludedOptions);
        $('v2-exclude-select')?.addEventListener('change', () => {
            closeOpportunityStream();
            state.excludedSymbols = [...document.querySelectorAll('#v2-exclude-select input[type="checkbox"]:checked')].map(input => input.value);
            state.selectedSymbols = state.selectedSymbols.filter(symbol => !state.excludedSymbols.includes(symbol));
            window.dispatchEvent(new CustomEvent('execution-selection-changed'));
            savePrefs();
            renderExcludedCatalog();
            renderSymbolCatalog();
            updateHistoryVisibility();
            scan(false);
        });
        $('v2-scan')?.addEventListener('click', () => scan(false));
        $('v2-scan-best')?.addEventListener('click', scanBest);
        $('notional')?.addEventListener('change', () => { if (state.selectedVenues.length >= 2) scan(false); });
        $('v2-auto')?.addEventListener('click', () => { state.auto = !state.auto; $('v2-auto').textContent = '自动更新：' + (state.auto ? '开' : '关'); $('v2-auto').setAttribute('aria-pressed', String(state.auto)); startAuto(); });
        $('opportunity-drawer-close')?.addEventListener('click', closeDrawer);
        $('history-pair')?.addEventListener('change', event => {
            const value = event.target.value || '';
            const [buy, sell] = value.split('->');
            window.setHistoryPair?.(buy, sell, $('market-select')?.value);
        });
        document.addEventListener('keydown', event => { if (event.key === 'Escape') closeDrawer(); });
        $('market-select')?.addEventListener('change', () => scan(false));
        load().finally(startAuto);
    });
})();
