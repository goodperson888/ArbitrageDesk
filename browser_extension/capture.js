/* Read-only allowlist: never emit cookies, request headers, wallet addresses or signatures. */
(() => {
    const path = '/api/quotes/indicative';
    const manualPaths = new Set(['/api/quotes/indicative', '/api/orders/new/market', '/api/quotes/accept', '/api/orders/v2', '/api/trades', '/api/positions', '/api/transfers']);
    const diagnosticCodes = new Set();
    const diagnose = code => {
        if (diagnosticCodes.has(code)) return;
        diagnosticCodes.add(code);
        // Fixed codes only: never print responses, request bodies or exception text.
        console.warn('[Variational bridge]', code);
    };
    const allowed = value => {
        try { const u = new URL(value, location.origin); return u.origin === 'https://omni.variational.io' && u.pathname === path; }
        catch { return false; }
    };
    const manualPath = value => {
        try {
            const u = new URL(value, location.origin);
            return u.origin === 'https://omni.variational.io' && manualPaths.has(u.pathname) ? u.pathname : null;
        } catch { return null; }
    };
    const number = value => {
        if (value === null || value === undefined || value === '' || typeof value === 'boolean') return null;
        const result = Number(value);
        return Number.isFinite(result) ? result : null;
    };
    const parsedBody = body => {
        if (body && typeof body === 'object') return body;
        if (typeof body !== 'string' || body.length > 200000) return null;
        try { return JSON.parse(body); } catch { return null; }
    };
    const identifiers = async (row, safe) => {
        for (const key of ['quote_id','order_id','rfq_id','source_rfq','source_quote','id','company','pool_location']) {
            if (typeof row[key] !== 'string' || !row[key]) continue;
            const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(row[key]));
            safe[key + '_digest'] = Array.from(new Uint8Array(bytes)).map(b=>b.toString(16).padStart(2,'0')).join('').slice(0,16);
        }
    };
    const safeRows = async body => {
        const rows = Array.isArray(body) ? body : (Array.isArray(body?.result) ? body.result : []);
        const result = [];
        for (let row of rows.slice(0, 20)) {
            if (!row || typeof row !== 'object') continue;
            if (row.position_info && typeof row.position_info === 'object') row = {...row, ...row.position_info};
            const instrument = row.instrument && typeof row.instrument === 'object' ? row.instrument : {};
            const symbol = row.symbol || instrument.underlying;
            if (!['BTC', 'ETH'].includes(symbol)) continue;
            const safe = {symbol};
            for (const key of ['status','clearing_status','order_type','tif','side','role','trade_type']) {
                if (typeof row[key] === 'string' && row[key].length <= 80) safe[key] = row[key];
            }
            for (const key of ['qty','price','mark_price','value','avg_entry_price','upnl','rpnl','cum_funding']) {
                const value = number(row[key]);
                if (value !== null) safe[key] = value;
            }
            await identifiers(row, safe);
            result.push(safe);
        }
        const pagination = body?.pagination;
        const complete = (Array.isArray(body) || (pagination && 'next_page' in pagination &&
            pagination.next_page === null && (pagination.last_page?.offset || 0) === 0)) && rows.length <= 20;
        return {rows: result, row_count: rows.length, complete: Boolean(complete)};
    };
    const safeTransfers = async body => {
        const rows = Array.isArray(body) ? body : (Array.isArray(body?.result) ? body.result : Array.isArray(body?.rows) ? body.rows : []);
        const result = [];
        for (const row of rows.slice(0, 20)) {
            if (!row || typeof row !== 'object') continue;
            const safe = {};
            for (const key of ['asset','token','fee_asset','fee_currency','funding_asset','funding_currency',
                'transfer_type','transaction_type','type','status','clearing_status']) {
                if (typeof row[key] === 'string' && row[key].length <= 32 && /^[A-Za-z0-9_. -]+$/.test(row[key])) safe[key] = row[key];
            }
            for (const key of ['amount','qty','fee','fee_amount','funding_fee','funding_payment','funding_amount','settled_funding']) {
                const value = number(row[key]);
                if (value !== null) safe[key] = value;
            }
            await identifiers(row, safe);
            if (Object.keys(safe).length) result.push(safe);
        }
        const pagination = body?.pagination;
        const complete = Array.isArray(body) || (pagination && 'next_page' in pagination &&
            pagination.next_page === null && (pagination.last_page?.offset || 0) === 0);
        return {rows: result, row_count: rows.length, complete: Boolean(complete) && rows.length <= 20};
    };
    const safeManualBody = async (endpoint, method, phase, body) => {
        const parsed = parsedBody(body);
        if (endpoint === path) {
            if (!parsed || phase !== 'response') return null;
            const safe = {symbol: parsed.instrument?.underlying, qty: number(parsed.qty), bid:number(parsed.bid), ask:number(parsed.ask)};
            await identifiers(parsed, safe);
            return safe;
        }
        if (endpoint === '/api/orders/new/market' || endpoint === '/api/quotes/accept') {
            if (!parsed || typeof parsed !== 'object') return null;
            const safe = {
                quote_id_present: Boolean(parsed.quote_id || parsed.quote_id_present),
                side: ['buy', 'sell'].includes(parsed.side) ? parsed.side : null,
                max_slippage: number(parsed.max_slippage),
                is_reduce_only: typeof parsed.is_reduce_only === 'boolean' ? parsed.is_reduce_only : null,
            };
            if (phase === 'response') safe.rfq_id_present = Boolean(parsed.rfq_id || parsed.rfq_id_present);
            await identifiers(parsed, safe);
            return safe;
        }
        if (endpoint === '/api/transfers') return safeTransfers(parsed);
        return safeRows(parsed);
    };
    const publishManual = async (url, method, status, phase, body, captureId) => {
      try {
        const endpoint = manualPath(url);
        if (!endpoint || !['GET', 'POST'].includes(method)) return;
        if (phase === 'request' && (method === 'GET' || endpoint === path)) return;
        const safeBody = await safeManualBody(endpoint, method, phase, body);
        if (!safeBody) return;
        window.postMessage({type: 'VARIATIONAL_MANUAL_EVENT_V1', event: {
            url: 'https://omni.variational.io' + endpoint, method, status, phase, body: safeBody, capture_id: captureId
        }}, location.origin);
      } catch { diagnose('MANUAL_REDACTION_FAILED'); }
    };
    function publish(requestBody, responseBody) {
        try {
            const request = typeof requestBody === 'string' ? JSON.parse(requestBody) : requestBody;
            const instrument = request?.instrument;
            const quote = responseBody?.result || responseBody;
            if (!['BTC', 'ETH'].includes(instrument?.underlying) || instrument.instrument_type !== 'perpetual_future') return;
            if (!(Number(quote?.bid) > 0 && Number(quote?.ask) >= Number(quote?.bid) && Number(request.qty) > 0)) return;
            const page = {};
            const margin = quote.margin_requirements;
            if (margin && number(margin.estimated_fees_bid) !== null && number(margin.estimated_fees_ask) !== null) {
                page.estimated_fees_bid = number(margin.estimated_fees_bid);
                page.estimated_fees_ask = number(margin.estimated_fees_ask);
            }
            if (quote.qty_limits) {
                const limits = {};
                for (const side of ['bid','ask']) {
                    const source = quote.qty_limits[side] || {};
                    limits[side] = Object.fromEntries(['min_qty_tick','min_qty','max_qty'].map(k=>[k,number(source[k])]));
                }
                page.qty_limits = limits;
            }
            // Only attach page slippage when the visible form matches this RFQ.
            // This is read-only and never extracts account/wallet details.
            if (typeof document !== 'undefined') {
                // Use the visible period/rate together, never infer annualization
                // from the public API's ambiguous funding_rate unit.
                const visible = document.body?.innerText || '';
                const funding = [...visible.matchAll(/(\d+)\s*(?:时|小时|h|hours?)\s*(?:资金费率|Funding\s+Rate)[\s\S]{0,45}?([+-]?[\d.]+)\s*%(?:\s*\/\s*(\d{1,2}):(\d{2}):(\d{2}))?/gi)];
                if (funding.length === 1 && /\/perpetual\//.test(location.pathname || '') &&
                    location.pathname.endsWith('/'+instrument.underlying)) {
                    const hours = Number(funding[0][1]), rate = Number(funding[0][2]);
                    if (hours >= 1 && hours <= 8 && Number.isFinite(rate) && Math.abs(rate) <= 10) {
                        Object.assign(page,{page_funding_bps:Number((rate*100).toFixed(10)),page_funding_interval_s:hours*3600});
                        if (funding[0][3] !== undefined) {
                            const remaining=Number(funding[0][3])*3600+Number(funding[0][4])*60+Number(funding[0][5]);
                            if (remaining<=hours*3600 && Number(funding[0][4])<60 && Number(funding[0][5])<60)
                                page.page_funding_next_in_s=remaining;
                        }
                    }
                }
                const get = id => document.querySelector('[data-testid="'+id+'"]');
                const price = node => Number(node?.textContent.replace(/[$,\s]/g,''));
                const ask = get('ask-price-display'), bid = get('bid-price-display');
                const text = get('slippage-button')?.textContent || '';
                const est = text.match(/(?:Est(?:imated)?[.]?|估|预计|预估)(?:\s+(?:slippage|滑点))?\s*[:：]?\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*%/i), max = text.match(/(?:Max(?:imum)?[.]?|最大)\s*[:：]?\s*(\d+(?:\.\d*)?|\.\d+)\s*%/i);
                if (Number(get('quantity-input')?.value) === Number(request.qty) &&
                    get('input-mode-toggle')?.textContent.trim() === instrument.underlying &&
                    Math.abs(price(ask)-Number(quote.ask)) <= .011 && Math.abs(price(bid)-Number(quote.bid)) <= .011) {
                    const side = ask?.parentElement.getAttribute('aria-pressed') === 'true' ? 'buy' :
                        bid?.parentElement.getAttribute('aria-pressed') === 'true' ? 'sell' : null;
                    if (est && max && side) Object.assign(page, {page_estimated_slippage_bps:Number(est[1])*100,
                        page_max_slippage_bps:Number(max[1])*100,page_side:side});
                }
            }
            window.postMessage({type: 'VARIATIONAL_READ_ONLY_QUOTE_V1', quote: {
                source_url: 'https://omni.variational.io' + path,
                symbol: instrument.underlying, instrument_type: instrument.instrument_type,
                qty: String(request.qty), bid: String(quote.bid), ask: String(quote.ask),
                observed_at: new Date().toISOString(), timestamp: typeof quote.timestamp === 'string' ? quote.timestamp : null,
                quote_id_present: !!quote.quote_id, ...page
            }}, location.origin);
        } catch { diagnose('QUOTE_FORM_CAPTURE_FAILED'); }
    }
    const original = window.fetch;
    window.fetch = async function(input, init) {
        let url = typeof input === 'string' ? input : (input instanceof URL ? input.href : input?.url);
        const method = (init?.method || (typeof input !== 'string' ? input?.method : null) || 'GET').toUpperCase();
        const captureId = crypto.randomUUID();
        let requestBody = Promise.resolve(init?.body || null);
        if (!init?.body && typeof input?.clone === 'function' && (allowed(url) || manualPath(url))) {
            try { requestBody = input.clone().text().catch(()=>null); } catch { /* preserve original request */ }
        }
        requestBody.then(body=>publishManual(url, method, 0, 'request', body, captureId));
        const response = await original.apply(this, arguments);
        if (!url && response.url) url = response.url;
        const endpoint = manualPath(url);
        if (endpoint) {
            // Some request clients abort their signal immediately after consuming
            // the original response. Finish reading ONE clone before returning it,
            // so a caller's cleanup cannot cancel both asynchronous clone readers.
            // The original body, status and identity remain untouched.
            try {
                const data = await response.clone().json();
                if (allowed(url) && response.ok) publish(await requestBody, data);
                await publishManual(url, method, response.status, 'response', data, captureId);
            } catch (error) {
                diagnose(error?.name === 'AbortError' ? 'CAPTURE_BODY_ABORTED' :
                    error?.name === 'SyntaxError' ? 'CAPTURE_BODY_NOT_JSON' : 'CAPTURE_BODY_FAILED');
            }
        }
        return response;
    };
    const open = XMLHttpRequest.prototype.open;
    const send = XMLHttpRequest.prototype.send;
    const requests = new WeakMap();
    XMLHttpRequest.prototype.open = function(method, url) {
        requests.set(this, {allowed: allowed(url), url, method: String(method || 'GET').toUpperCase(), captureId:crypto.randomUUID()});
        return open.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function(body) {
        const metadata = requests.get(this);
        if (metadata) publishManual(metadata.url, metadata.method, 0, 'request', body || null, metadata.captureId);
        if (metadata?.allowed && typeof body === 'string') {
            this.addEventListener('load', () => {
                try { if (this.status >= 200 && this.status < 300) publish(body, this.responseType === 'json' ? this.response : JSON.parse(this.responseText)); }
                catch { /* Non-JSON or unsuccessful replies are not quotes. */ }
            }, {once: true});
        }
        if (metadata && manualPath(metadata.url)) {
            this.addEventListener('load', () => {
                try {
                    const value = this.responseType === 'json' ? this.response : JSON.parse(this.responseText);
                    publishManual(metadata.url, metadata.method, this.status, 'response', value, metadata.captureId);
                } catch { /* Non-JSON replies are ignored. */ }
            }, {once: true});
        }
        return send.apply(this, arguments);
    };
})();
