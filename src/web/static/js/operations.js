/* Read-only operations view. Every external string is rendered as text. */
(() => {
    const get = id => document.getElementById(id);
    const setDisabled = (id, value) => {
        const element = get(id);
        if (element) element.disabled = value;
    };
    const bind = (id, event, handler) => {
        const element = get(id);
        if (element) element[event] = handler;
    };
    async function readJsonResponse(response, label='接口') {
        let body='';
        if(typeof response.text==='function') body=await response.text();
        else if(typeof response.json==='function') return response.json();
        let data;
        try { data=body?JSON.parse(body):{}; }
        catch(error) {
            const status=response.status?`HTTP ${response.status}`:'未知状态';
            if(/text\/html/i.test(response.headers?.get?.('content-type')||'')||/^\s*<!doctype\s+html|^\s*<html[\s>]/i.test(body))
                throw new Error(`${label}返回了 HTML 页面（${status}），不是 JSON；请确认面板和 API 使用同一个本地服务并刷新页面`);
            throw new Error(`${label}返回了无效 JSON（${status}）`);
        }
        if(response.ok===false){
            if(response.status===403 && data.error==='本机页面已失效，请刷新后重试'){
                // A service restart rotates the local write token. Refresh
                // once so an old tab cannot leave every control looking dead.
                window.setTimeout(()=>window.location.reload(),50);
                throw new Error('本机页面已失效，正在刷新页面后重试');
            }
            throw new Error(data.error||`${label}请求失败（HTTP ${response.status||'未知'}）`);
        }
        return data;
    }
    let pending = false, strategyDirty = false;
    const strategyControlIds = ['position-mode','grid-step','grid-max-layers','grid-layer-notional','grid-total-notional','position-size','max-positions','leverage','second-leg-retries','loss-limits','slippage','max-trades','execution-entry-mode','execution-entry-quantile','execution-entry-min-samples','execution-target-window','execution-funding-reserve','execution-funding-mode','execution-funding-hold','execution-funding-extra','execution-close-target','min-spread','max-spread','execution-safety-margin','execution-price-movement','hedge-max-delay','route-rollback-limit','route-rollback-cooldown','close-mode','close-convergence-basis','close-convergence-bps','close-min-profit-usdc','close-min-profit-bps','close-cost-reserve-usdc','close-max-hold-minutes','max-total-position-notional'];
    strategyControlIds.push('book-utilization','channel-method','channel-window','channel-quantile','channel-multiplier','channel-min-samples','channel-min-width','channel-max-width','channel-lower','channel-upper','channel-exit-mode','channel-reversion');
    strategyControlIds.push('capital-allocation-mode','capital-reserve-percent','capital-reserve-usdc');
    const CHECK_WINDOW_MS = 120000;
    let checkedUntil = 0, checking = false, armed = false, stopped = false, running = false, stopRequested = false;
    let latestOperations = null, checkedScopeKey = null, checkedExecutionPair = null;
    let savedEntryLimits = null;
    let scopeTimer = null, scopeSubmitting = false, scopeAgain = false;
    let scopeNotice = null;
    const runtimeScope = execution => execution?.position_monitor?.runtime_scope;

    function renderEntryScopeControl(execution = latestOperations?.execution) {
        const button = get('execution-apply-scope'), result = get('execution-scope-result');
        if (!button) return;
        const scope = runtimeScope(execution), differs = platformScope(execution).differs;
        button.hidden = !execution?.running || execution.mode !== 'continuous_monitor' || execution.close_only || !differs;
        button.disabled = scopeSubmitting || !scope || scope.version !== 1 || execution.position_monitor?.heartbeat_stale;
        button.title = scope?.version === 1 ? '后台核验后应用当前参与平台，已有仓位保持原平台；不会解除停止'
            : '当前旧进程继续监控；带仓切换在下一次启动实盘时加载';
        if (scopeNotice && (!execution?.running || execution.pid !== scopeNotice.pid ||
                execution.started_at !== scopeNotice.started_at ||
                (scopeNotice.id && scope?.request_id === scopeNotice.id && scope?.state !== 'preparing'))) scopeNotice = null;
        if (result) result.textContent = scopeNotice?.message || (!execution?.running ? '' : scope?.version === 1
            ? scope.message || '' : differs ? button.title : '');
        if (result && execution?.running && execution.position_monitor?.heartbeat_stale)
            result.textContent = '监控心跳已过期，未确认新范围生效；'+result.textContent;
    }

    async function applyEntryScope() {
        if (scopeSubmitting) { scopeAgain = true; return; }
        const execution = latestOperations?.execution;
        if (!execution?.running || execution.mode !== 'continuous_monitor' || execution.close_only) return;
        const result = get('execution-scope-result');
        if (runtimeScope(execution)?.version !== 1) {
            renderEntryScopeControl(execution);
            return;
        }
        const venues = globalThis.getSelectedVenues?.() || [], symbols = executionSelection().symbols || [];
        if (venues.length < 2 || !symbols.length) {
            if (result) result.textContent = '等待新平台的共同品种目录；原实盘范围继续运行。';
            return;
        }
        scopeSubmitting = true;
        scopeNotice = {pid:execution.pid,started_at:execution.started_at,
            message:'正在后台核验新仓平台，旧仓继续监控…'};
        renderEntryScopeControl();
        if (result) result.textContent = '正在后台核验新仓平台，旧仓继续监控…';
        const controller = new AbortController(), timer = setTimeout(()=>controller.abort(),10000);
        try {
            const response = await fetch('/api/execution/scope', {method:'POST',signal:controller.signal,
                headers:{'Content-Type':'application/json','X-Local-Token':document.querySelector('meta[name="local-token"]').content},
                body:JSON.stringify({pid:execution.pid,started_at:execution.started_at,venues,symbols})});
            const data = await readJsonResponse(response, '应用新仓平台');
            if (!data.success) throw new Error(data.message || '范围更新未通过');
            scopeNotice = {pid:execution.pid,started_at:execution.started_at,id:data.request_id,message:data.message};
            if (result) result.textContent = data.message;
        } catch (error) {
            const message = error.name === 'AbortError'
                ? '范围更新请求超时，请以后台实际生效范围为准。' : '新仓范围未切换：'+error.message;
            scopeNotice = {pid:execution.pid,started_at:execution.started_at,message};
            if (result) result.textContent = message;
        } finally {
            clearTimeout(timer);
            scopeSubmitting = false;
            renderEntryScopeControl();
            if (scopeAgain) { scopeAgain = false; await applyEntryScope(); }
        }
    }

    function renderResumeControl(execution = latestOperations?.execution) {
        const button = get('execution-resume');
        if (!button) return;
        const visible = execution?.running && execution.stop_requested &&
            execution.mode === 'continuous_monitor' && !execution.close_only;
        button.hidden = !visible;
        const control = execution?.resume_control;
        button.disabled = checking || strategyDirty || !control?.available;
        button.title = strategyDirty ? '请先保存策略，再恢复新开仓' : control?.available
            ? '恢复 '+venueListText(platformScope(execution).active)+
                (runtimeScope(execution)?.version===1?' 新仓范围':' 原运行范围')+'；按已保存上限开新仓，已有仓位继续监控'
            : control?.reason || '等待原运行状态核验';
    }

    function renderEntryLimits(execution = latestOperations?.execution) {
        const node = get('entry-limits-status');
        if (!node) return;
        const saved = savedEntryLimits;
        const active = execution?.position_monitor?.entry_limits;
        const describe = value => `${value.max_positions} 组 / 总额 ${Number(value.max_total_position_notional)>0 ? value.max_total_position_notional+' USDC' : '不限'} / 每腿 ${value.position_size} USDC`+
            (Number(value.capital_reserve_percent)||Number(value.capital_reserve_usdc)||value.capital_allocation_mode==='remaining_slots'
                ? ` / 每平台预留 max(${value.capital_reserve_percent||0}%, ${value.capital_reserve_usdc||0} U)`+(value.capital_allocation_mode==='remaining_slots'?' / 按剩余名额分配':'') : '');
        if (!execution?.running) {
            node.textContent = saved ? '已保存：'+describe(saved)+'；下次启动使用。' : '持仓上限：正在读取配置…';
        } else if (!active || active.version !== 1) {
            node.textContent = (saved ? '已保存：'+describe(saved)+'。' : '')+'当前进程未上报动态上限；需在旧仓平仓、本次执行结束后启动新版。';
        } else {
            const pending = saved && (Number(saved.max_positions)!==Number(active.max_positions) ||
                Number(saved.max_total_position_notional)!==Number(active.max_total_position_notional) ||
                Number(saved.position_size)!==Number(active.saved_position_size) ||
                ['capital_reserve_percent','capital_reserve_usdc'].some(key=>Number(saved[key]||0)!==Number(active[key]||0)) ||
                (saved.capital_allocation_mode||'per_leg')!==(active.capital_allocation_mode||'per_leg'));
            node.textContent = `${execution.position_monitor.heartbeat_stale ? '上次上报（心跳待核验）' : '本次生效'}：${describe(active)}；当前 ${active.active_groups} 组。`+
                (pending ? ' 已保存：'+describe(saved)+'；等待下次开仓检查应用。' : '')+
                (active.single_position_cycle ? ' 当前仍为旧单仓循环；修改并保存最大组数后切换为按上限运行。' : '')+
                (active.execution_notional_cap!=null ? ` 本次每腿仍不超过启动上限 ${active.execution_notional_cap} USDC。` : '')+
                (active.pending_other_settings ? ' 其他策略修改待全部平仓后生效。' : '')+
                (pending && active.capital_policy_version!==1 && (saved.capital_reserve_percent || saved.capital_reserve_usdc || saved.capital_allocation_mode==='remaining_slots')
                    ? ' 当前工作进程未加载资金预留功能；请在本次执行安全结束后启动新版。' : '')+
                (execution.close_only || execution.stop_requested ? ' 本次已暂停新开仓，保存上限不会恢复开仓。' : '');
            if (execution.mode && execution.mode !== 'continuous_monitor') node.textContent += ' 本次为单次验证，不会因调高上限追加组。';
        }
        if (strategyDirty) node.textContent += ' 表单有未保存修改。';
    }
    // Only an explicit mode selection changes the next launch policy. Browser
    // form restoration and diagnostics polls are not user launch instructions.
    let requestedCloseOnly = false;
    const MARKET_API = 'http://127.0.0.1:5002/api/v2';
    const minimumVenueNames = {variational:'Variational', lighter:'Lighter 主网', lighter_rh:'Lighter RH', standx:'StandX', arcus:'Arcus'};
    const selectedExecutionPair = (selected = globalThis.getSelectedVenues?.() || []) => {
        if (selected.length === 2) return selected.slice();
        if (selected.length > 2) {
            const best = globalThis.getBestExecutionVenues?.({requirePositive:false});
            // A visible opportunity is helpful for ordering, but it is not a
            // prerequisite for the read-only check.  The backend will check
            // every selected pair and bind the first route that passes.
            return Array.isArray(best) && best.length === 2 ? best.slice() : selected.slice(0, 2);
        }
        return null;
    };
    const minimumState = {key:'', loadedAt:0, pending:false, data:null, requestId:0};
    const minimumCatalogCache = new Map();
    const minimumQuoteCache = new Map();
    const symbolBase = value => String(value || '').toUpperCase().replace(/-PERP$/, '');
    const finiteNumber = value => {
        if (value == null || typeof value === 'boolean' || typeof value === 'string' && !value.trim()) return null;
        const number = Number(value);
        return Number.isFinite(number) ? number : null;
    };
    const formatUsdc = value => finiteNumber(value) == null ? null : Number(value).toLocaleString('en-US', {maximumFractionDigits:2});
    const formatBase = value => finiteNumber(value) == null ? null : Number(value).toLocaleString('en-US', {maximumFractionDigits:8});
    function minimumContext() {
        const selection = executionSelection();
        const best = globalThis.getBestExecutionOpportunity?.();
        const selected = globalThis.getSelectedVenues?.() || [];
        const symbol = selection.mode === 'selected' && selection.symbols?.[0]
            ? symbolBase(selection.symbols[0])
            : best?.canonical_symbol ? symbolBase(best.canonical_symbol) : symbolBase(selection.symbols?.[0]);
        // Scanning may use many venues. When an execution check is needed,
        // bind it to the current best fresh pair and keep that pair fixed for
        // the worker lifetime.
        let venues = selectedExecutionPair(selected) || selected.slice();
        return {symbol, venues:[...new Set(venues.filter(Boolean))], best};
    }
    async function marketJson(path, timeoutMs = 6000) {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), timeoutMs);
        try {
            const response = await fetch(MARKET_API + path, {cache:'no-store', signal:controller.signal});
            if (!response.ok) throw new Error('HTTP ' + response.status);
            return response.json();
        } finally { clearTimeout(timer); }
    }
    function instrumentFor(rows, symbol) {
        const target = symbolBase(symbol);
        return (Array.isArray(rows) ? rows : []).find(row =>
            symbolBase(row.canonical_symbol || row.venue_symbol) === target && row.enabled !== false) || null;
    }
    function quotePriceFor(venue, quote, best) {
        if (best) {
            if (venue === best.buy_venue) return finiteNumber(best.buy_price);
            if (venue === best.sell_venue) return finiteNumber(best.sell_price);
        }
        const row = Array.isArray(quote) ? quote[0] : quote;
        if (!row) return null;
        return finiteNumber(row.ask) || finiteNumber(row.bid) || finiteNumber(row.mark_price);
    }
    function estimateVenueMinimum(instrument, price) {
        if (!instrument) return {known:false};
        const minBase = finiteNumber(instrument.min_base_amount);
        const minQuote = finiteNumber(instrument.min_quote_amount);
        const decimals = finiteNumber(instrument.size_decimals);
        let requiredBase = Math.max(minBase || 0, price && minQuote != null ? minQuote / price : 0);
        const step = decimals == null ? finiteNumber(instrument.metadata?.step_size) : 10 ** (-Math.max(0, Math.floor(decimals)));
        if (requiredBase > 0 && step != null && step > 0) {
            requiredBase = Math.ceil((requiredBase - 1e-12) / step) * step;
        }
        const byBase = price && requiredBase > 0 ? requiredBase * price : null;
        const effective = Math.max(minQuote || 0, byBase || 0);
        return {known: effective > 0 || minBase != null || minQuote != null,
            minBase, minQuote, price, requiredBase: requiredBase > 0 ? requiredBase : null,
            effective: effective > 0 ? effective : null};
    }
    function renderMinimumHint(data = minimumState.data) {
        const hint = get('execution-cap-hint');
        if (!hint) return;
        if (!data) {
            const context = minimumContext();
            hint.textContent = context.venues.length < 2 ? '至少选择两个平台后自动读取最低开仓门槛' : '正在读取选中平台的最低开仓门槛…';
            hint.title = '';
            return;
        }
        const venueText = data.entries.map(entry => {
            const name = minimumVenueNames[entry.venue] || entry.venue;
            if (!entry.estimate.known) return name + '：最低限制待检查';
            const amount = entry.estimate.effective == null ? null : formatUsdc(entry.estimate.effective) + ' U';
            const base = entry.estimate.requiredBase == null ? null : '数量 ≥ ' + formatBase(entry.estimate.requiredBase);
            return name + '：' + (amount || base || '最低限制待检查') + (amount && base ? '（' + base + '）' : '');
        });
        const known = data.entries.map(entry => entry.estimate.effective).filter(value => value != null);
        const cap = finiteNumber(get('execution-cap')?.value);
        let text = (data.symbol ? data.symbol + ' · ' : '') + venueText.join('；');
        if (data.effective != null) {
            text += data.exactPair ? '；两腿有效最低约 ' : '；选中平台中最高最低约 ';
            text += formatUsdc(data.effective) + ' USDC';
            text += cap != null && cap + 1e-9 < data.effective
                ? data.exactPair ? ' · 当前上限低于最低门槛，检查不会通过' : ' · 当前上限低于部分平台门槛，实际配对时会重新核对'
                : data.exactPair ? ' · 当前上限是可执行上限，最终按实时盘口自动缩量' : ' · 启动时按实际两腿重新核对，最终按实时盘口自动缩量';
        } else if (known.length === 0) {
            text += '；精确金额待指定品种报价后确认，检查时会再次核对；不会自动提高上限';
        } else {
            text += '；部分平台最低限制待检查，检查时会再次核对；不会自动提高上限';
        }
        hint.textContent = data.effective != null
            ? `${data.exactPair ? '两腿最低约 ' : '选中平台最高最低约 '}${formatUsdc(data.effective)} USDC`+
              (cap != null && cap + 1e-9 < data.effective ? ' · 上限偏低，见详情' : ' · 可按盘口缩量')
            : '最低门槛待只读检查';
        hint.title = text + '。最低门槛按数量精度和实时价格估算；账户可用保证金仍需只读检查。';
    }
    function syncExecutionCapLimit() {
        const execution = latestOperations?.execution;
        const active = execution?.running ? execution.position_monitor?.entry_limits : null;
        const executionCap = get('execution-cap'), toggle = get('execution-temporary-cap');
        const label = get('execution-global-cap'), field = get('execution-cap-override');
        // Only saved configuration authorizes a cap. Unsaved settings are a draft.
        const savedCap = finiteNumber(savedEntryLimits?.position_size ?? latestOperations?.notional_cap ?? executionCap?.max ?? executionCap?.value);
        if (execution?.running && toggle) {
            toggle.checked = active ? active.execution_notional_cap != null : execution.follow_saved_cap !== true;
        }
        const temporary = toggle?.checked === true;
        if (toggle) toggle.disabled = checking || execution?.running === true;
        if (field) field.hidden = !temporary;
        if (executionCap) {
            executionCap.disabled = checking || execution?.running === true || !temporary;
            if (savedCap != null && savedCap >= 1) {
                executionCap.max = String(savedCap);
                const current = finiteNumber(executionCap.value);
                const next = execution?.running && temporary
                    ? finiteNumber(active?.execution_notional_cap ?? execution.max_notional)
                    : !temporary || current == null || current > savedCap ? savedCap : current;
                if (next != null && current !== next) {
                    executionCap.value = String(next);
                    if (!execution?.running) invalidate('已保存金额已同步，请重新执行只读检查。');
                }
                executionCap.title = `本次最多 ${formatUsdc(savedCap)} USDC；按已保存设置限制`;
            }
        }
        if (label) {
            if (active && Number.isFinite(active.effective_entry_cap)) {
                label.textContent = `${execution.position_monitor.heartbeat_stale ? '上次上报' : '新仓上限'} ${formatUsdc(active.effective_entry_cap)} USDC · ${(active.limiting_sources || []).join(' / ') || '每腿设置'}`;
            } else if (execution?.running && temporary) {
                label.textContent = `启动固定上限 ${formatUsdc(active?.execution_notional_cap ?? execution.max_notional ?? savedCap)} USDC`;
            } else {
                label.textContent = savedCap == null ? '等待已保存设置…' : temporary
                    ? `已保存上限 ${formatUsdc(savedCap)} USDC`
                    : `跟随设置 · ${formatUsdc(savedCap)} USDC`;
            }
        }
    }
    async function refreshMinimumHint(force = false) {
        const context = minimumContext();
        if (context.venues.length < 2) { minimumState.data = null; minimumState.key = ''; renderMinimumHint(null); return; }
        const key = context.venues.join(',') + '|' + context.symbol + '|' + (context.best?.buy_venue || '') + '>' + (context.best?.sell_venue || '');
        if (!force && minimumState.data && minimumState.key === key && Date.now() - minimumState.loadedAt < 30000) {
            renderMinimumHint();
            return;
        }
        if (minimumState.pending && minimumState.key === key) return;
        minimumState.key = key; minimumState.pending = true; const requestId = ++minimumState.requestId;
        renderMinimumHint(null);
        try {
            const entries = await Promise.all(context.venues.map(async venue => {
                const catalogKey = venue;
                const cachedCatalog = minimumCatalogCache.get(catalogKey);
                const instrumentRows = cachedCatalog && Date.now() - cachedCatalog.loadedAt < 30000
                    ? cachedCatalog.rows
                    : await marketJson('/instruments?venue=' + encodeURIComponent(venue)).then(rows => {
                        minimumCatalogCache.set(catalogKey, {rows, loadedAt:Date.now()});
                        return rows;
                    });
                const instrument = context.symbol ? instrumentFor(instrumentRows, context.symbol) : null;
                let quotes = null;
                if (context.symbol) {
                    const quoteKey = venue + '|' + context.symbol;
                    const cachedQuote = minimumQuoteCache.get(quoteKey);
                    try {
                        quotes = cachedQuote && Date.now() - cachedQuote.loadedAt < 5000
                            ? cachedQuote.rows
                            : await marketJson('/quotes?symbol=' + encodeURIComponent(context.symbol) + '&venue=' + encodeURIComponent(venue)).then(rows => {
                                minimumQuoteCache.set(quoteKey, {rows, loadedAt:Date.now()});
                                return rows;
                            });
                    }
                    catch (_) { /* The instrument minimum remains useful without a quote. */ }
                }
                const price = quotePriceFor(venue, quotes, context.best);
                return {venue, instrument, estimate:estimateVenueMinimum(instrument, price)};
            }));
            if (requestId !== minimumState.requestId) return;
            const effectiveValues = entries.map(entry => entry.estimate.effective).filter(value => value != null);
            minimumState.data = {symbol:context.symbol, entries, effective:effectiveValues.length ? Math.max(...effectiveValues) : null,
                exactPair: context.venues.length === 2 || Boolean(context.best)};
            minimumState.loadedAt = Date.now();
            renderMinimumHint();
        } catch (_) {
            if (requestId === minimumState.requestId) {
                minimumState.data = null;
                renderMinimumHint(null);
                const hint = get('execution-cap-hint');
                if (hint && context.venues.length >= 2) hint.textContent = '平台最低门槛暂未读取成功；只读检查时会再次核对，不会自动提高上限';
            }
        } finally {
            if (requestId === minimumState.requestId) minimumState.pending = false;
        }
    }
    let checkState = 'idle';
    let observerBusy=false;
    async function openVariational(){
        const button=get('browser-open');if(button.disabled)return;
        button.disabled=true;
        get('browser-open-result').textContent='正在查找已有会话或打开专用浏览器…';
        try{
            const symbol = executionSelection().symbols?.[0] || 'ETH';
            const r=await fetch('/api/execution/browser-open',{method:'POST',headers:{'Content-Type':'application/json','X-Local-Token':document.querySelector('meta[name="local-token"]').content},body:JSON.stringify({symbol})});
            const data=await readJsonResponse(r,'浏览器连接');if(data.error)throw new Error(data.error);
            get('browser-open-result').textContent=data.message;
            invalidate('浏览器已连接；请确认官网登录后准备执行会话，再做只读检查。');
        }catch(error){get('browser-open-result').textContent=error.message;}
        finally{button.disabled=false;await refresh();}
    }
    async function observerAction(action){
        if(observerBusy)return;
        observerBusy=true;
        setDisabled('quote-observer-start', true);setDisabled('quote-observer-stop', true);
        try{
            const r=await fetch('/api/quote-observer/'+action,{method:'POST',headers:{'X-Local-Token':document.querySelector('meta[name="local-token"]').content}});
            const data=await readJsonResponse(r,'报价监听接口');
            if(data.error)throw new Error(data.error);
            get('quote-observer-status').textContent=data.message;
        }catch(error){get('quote-observer-status').textContent=error.message;}
        finally{observerBusy=false;await refresh();}
    }
    const armStatus = text => {if(get('execution-arm-status'))get('execution-arm-status').textContent=text;};
    const liveLabel = text => {
        const label = get('live-arm-label');
        if (label) label.textContent = text;
        else if (get('live-arm')) get('live-arm').textContent = text;
    };
    const drainStatus = execution => {
        if (execution?.recovery_blocked) return {
            label:'已停止开仓 · 原仓位待核对',
            detail:'已停止开仓；原仓位仍待核对，尚未确认接管。进程退出后请点击“核对原仓位（只读）”，通过后重新检查并开启实盘接管。'
        };
        const reported = execution?.tracked_position_count;
        const count = Number.isInteger(reported) && reported >= 0 ? reported
            : execution?.managed_positions?.length > 0 ? execution.managed_positions.length : null;
        const label = count > 0 ? '已停止开仓 · 正在管理剩余 '+count+' 笔持仓'
            : count === 0 ? '已停止开仓 · 等待确认退出' : '已停止开仓 · 正在核对剩余持仓';
        const detail = label+'。'+(count === 0
            ? '当前本地无待管理仓位，等待执行进程完成收尾并退出；账户状态以核对结果为准。'
            : '已有仓位继续按平仓条件和保护规则监控，不会立即强制平仓；确认空仓后进程退出。');
        return {label, detail};
    };
    const executionSelection = () => {
        const selected = globalThis.getExecutionSelection?.();
        if (selected && Array.isArray(selected.symbols)) return selected;
        return {mode: 'all', symbols: []};
    };
    const venueListText = venues => (venues || []).map(venue => minimumVenueNames[venue] || venue).join(' ↔ ') || '组合待核对';
    const symbolSet = values => [...new Set((values || []).map(symbolBase).filter(Boolean))].sort();
    const scopeKey = () => {
        const scope = globalThis.getExecutionScope?.() || executionSelection();
        const mode = scope.mode === 'all' ? 'all' : 'selected';
        // Market ranking changes independently of the user's authorization.
        // The checked pair is retained separately for the next launch request.
        return JSON.stringify({venues:(globalThis.getSelectedVenues?.() || []).slice().sort(),
            selection:{mode, symbols:mode === 'all' ? [] : symbolSet(scope.symbols),
                excluded_symbols:symbolSet(scope.excluded_symbols)},
            cap:Number(get('execution-cap')?.value), policy:executionPolicy(),
            strategy:strategyControlIds.map(id=>[id,String(get(id)?.value??'')])});
    };
    const platformScope = execution => {
        const selected = globalThis.getSelectedVenues?.() || [];
        const active = runtimeScope(execution)?.entry_venues || execution?.venues || [];
        return {selected, active, differs: Boolean(execution?.running &&
            (active.length !== selected.length || active.some(venue => !selected.includes(venue))))};
    };
    function renderPlatformScope(execution = latestOperations?.execution) {
        const node = get('execution-platform-state');
        if (!node) return;
        const scope = platformScope(execution);
        const liveScope = runtimeScope(execution);
        if (execution?.running && liveScope?.version === 1) {
            node.textContent = (execution.position_monitor?.heartbeat_stale?'最近上报新仓范围（心跳待恢复）：':'后续新仓：')+venueListText(scope.active)+
                (scope.differs?'；当前改选：'+venueListText(scope.selected)+'，等待后台核验及应用。':'。')+
                '已有仓位始终在各自原平台管理；调整参与平台会后台核验后用于后续新仓。';
            return;
        }
        node.textContent = execution?.running
            ? '正在执行：'+venueListText(scope.active)+'（启动时固定）；当前行情选择：'+venueListText(scope.selected)+
              (scope.differs ? '，尚未切换实盘。' : '。')+
              ' 切换实盘组合：先停止旧组合新开仓，旧仓仍在原平台按规则平仓；确认空仓且进程退出后，核对账户并解除停止，再对新组合只读检查并手动启动。'
        : scope.selected.length > 2
            ? '下一次执行：从 '+venueListText(scope.selected)+' 的新鲜机会中绑定最优两腿；绑定后本轮运行固定，不会在首腿成交后换平台。'
            : '下一次执行选择：'+venueListText(scope.selected)+'。修改参与平台只改变行情扫描，并使上次只读检查失效；实盘组合在启动时固定。';
    }
    const executionPolicy = () => {
        const selection = executionSelection();
        return {
            target_notional: Number(get('execution-target')?.value || 0),
            follow_saved_cap: get('execution-temporary-cap')?.checked !== true,
            candidate_fallback: get('execution-fallback')?.checked !== false,
            single_position_cycle: false,
            close_only: running ? latestOperations?.execution?.close_only === true : requestedCloseOnly,
            gross_spread_min_bps: Number(selection.gross_spread_min_bps || 0),
            gross_spread_max_bps: Number(selection.gross_spread_max_bps || 0),
            reversion_target_mode: selection.reversion_target_mode || 'center',
            reversion_window_seconds: Number(selection.reversion_window_seconds || 604800),
            reversion_min_count: Number(selection.reversion_min_count || 0),
            reversion_min_deviation_bps: Number(selection.reversion_min_deviation_bps || 0),
            reversion_max_age_seconds: Number(selection.reversion_max_age_seconds || 0),
            reversion_recency_mode: selection.reversion_recency_mode || 'hard',
            reversion_min_samples: Number(selection.reversion_min_samples || 0)
        };
    };
    const syncExecutionMode = () => {
        const control=get('execution-trade-mode'), help=get('execution-trade-mode-help');
        const closeOnly=executionPolicy().close_only;
        if(control){
            control.value=closeOnly?'close_only':'open_close';
            control.disabled=checking||running;
        }
        if(help)help.textContent=running
            ? '当前运行：'+(closeOnly?'仅管理已有仓位，不开新仓；此模式需在本次结束后重新选择。':
                stopRequested?'已停止新开仓；可点击“恢复新开仓”继续原运行范围。':'自动开仓与平仓。')+
                ' 同时持仓组数和金额上限可在设置中保存，运行中生效。'
            : '下次启动：'+(closeOnly?'仅管理已有仓位，不开新仓。':'自动开仓与平仓，达到入场门槛才会提交。')+' 此选择只在检查后手动启动时生效。';
    };
    const syncExecutionPolicy = () => {
        const plan = get('execution-plan');
        if (!plan) return;
        const policy = executionPolicy();
        const target = policy.target_notional > 0 ? `单腿开仓累计目标 ${policy.target_notional} USDC（达目标停新开仓）` : '累计目标不限';
        const spread = policy.gross_spread_max_bps > 0
            ? `毛价差 ${(policy.gross_spread_min_bps / 100).toFixed(4)}%～${(policy.gross_spread_max_bps / 100).toFixed(4)}%`
            : `毛价差 ≥ ${(policy.gross_spread_min_bps / 100).toFixed(4)}%`;
        plan.textContent = policy.close_only
            ? '仅管理已有仓位：按平仓规则监控，不扫描或提交新入场订单；结束本次运行时，已有仓位继续按规则处理，确认结束后退出。'
            : `自动开仓与平仓 · ${policy.candidate_fallback ? '候选失败自动切换' : '候选失败停止'} · 回归净收益优先排序 · ${get('position-mode')?.value==='spread_grid' ? '价差网格，按层数和总额运行' : policy.single_position_cycle ? '单仓循环，平仓后下一单' : '按并发上限运行'} · ${spread} · ${target}；单次验证完成一轮开仓→平仓后结束`;
    };
    const strategyField = id => get(id + '-field') || get(id)?.closest?.('label');
    const setStrategyFieldVisible = (id, visible) => {
        const field = strategyField(id);
        if (field) field.hidden = !visible;
    };
    const syncStrategyFieldVisibility = () => {
        const value = id => get(id)?.value ?? '';
        const grid = value('position-mode') === 'spread_grid';
        ['grid-step','grid-layer-notional','grid-max-layers','grid-total-notional'].forEach(id=>setStrategyFieldVisible(id,grid));
        if(get('grid-policy-help')) get('grid-policy-help').hidden=!grid;
        const entryAdaptive = value('execution-entry-mode') === 'adaptive_quantile';
        const channel = value('execution-entry-mode') === 'channel';
        setStrategyFieldVisible('execution-entry-quantile', entryAdaptive);
        setStrategyFieldVisible('execution-entry-min-samples', entryAdaptive);

        const closeMode = value('close-mode');
        setStrategyFieldVisible('close-mode',!channel);
        const usesConvergence = ['convergence', 'convergence_and_profit', 'convergence_or_profit'].includes(closeMode);
        const usesProfit = true;
        const remainingBasis = true;
        setStrategyFieldVisible('close-convergence-basis', false);
        setStrategyFieldVisible('close-convergence-bps', usesConvergence || channel);
        setStrategyFieldVisible('execution-close-target', !channel);
        setStrategyFieldVisible('execution-target-window', !channel && (value('execution-close-target') !== 'zero' || entryAdaptive));
        setStrategyFieldVisible('close-min-profit-usdc', usesProfit);
        setStrategyFieldVisible('close-min-profit-bps', usesProfit);
        setStrategyFieldVisible('close-cost-reserve-usdc', usesProfit);

        const bpsLabel = get('close-convergence-bps-label');
        const bpsHelp = get('close-convergence-bps-help');
        if (bpsLabel) bpsLabel.textContent = remainingBasis ? '目标容差 / bps' : '最少收窄幅度 / bps';
        if (bpsHelp) bpsHelp.textContent = remainingBasis
            ? '接近本轮冻结目标的容差；越过目标也算收敛，仍须满足实际净盈利；1 bps = 0.01%'
            : '相对入场价差至少收窄的幅度；1 bps = 0.01%';
    };
    const syncStrategySummary = () => {
        syncStrategyFieldVisibility();
        const entryPreview = get('entry-threshold-preview');
        if (entryPreview) {
            const minimum = Number(get('min-spread')?.value);
            const reserve = Number(get('execution-safety-margin')?.value);
            const maximum = Number(get('max-spread')?.value);
            const format = n => Number(n.toFixed(6)).toString();
            const complete = ['min-spread','execution-safety-margin','max-spread'].every(id => String(get(id)?.value ?? '').trim() !== '');
            if (complete && [minimum, reserve, maximum].every(n => Number.isFinite(n) && n >= 0)) {
                const firstMinimum = minimum + reserve;
                const conflict = firstMinimum > maximum + 1e-7;
                entryPreview.textContent = `预计净回归空间：首腿前 ${format(minimum)} + 预留 ${format(reserve)} = ${format(firstMinimum)} bps（${format(firstMinimum / 100)}%）起；首腿成交后补齐底线 ${format(minimum)} bps。`
                    + (conflict ? ' 当前首腿门槛超过净回归空间上限，无法入场。' : '')
                    + (get('execution-entry-mode')?.value === 'adaptive_quantile' ? ' 自适应分位条件还需同时满足。' : '');
                entryPreview.classList?.toggle('warn', conflict);
            } else {
                entryPreview.textContent = '请填写有效的最低净回归空间、入场预留和净空间上限。';
                entryPreview.classList?.toggle('warn', true);
            }
        }
        const summary = get('execution-strategy-summary');
        const detail = get('execution-strategy-summary-detail');
        if (!summary && !detail) return;
        const value = id => get(id)?.value ?? '';
        const numberText = (id, fallback='0') => {
            const raw = value(id);
            return raw === '' ? fallback : raw;
        };
        const entryMode = value('execution-entry-mode');
        const quantile = Number(value('execution-entry-quantile'));
        const inverse = Number.isFinite(quantile) ? Math.max(0, 100 - quantile) : 10;
        const entry = entryMode === 'channel' ? '通道触边 + 整轮净收益门槛（目标按入场冻结）' : entryMode === 'adaptive_quantile'
            ? `历史分位 P${quantile || 90} / P${inverse} + 净回归空间门槛`
            : `首腿前净回归空间 ≥ ${numberText('min-spread')} + 预留 ${numberText('execution-safety-margin')} bps`;
        const grid = value('position-mode') === 'spread_grid';
        const channel = entryMode === 'channel';
        const closeMode = channel ? '通道冻结目标且净盈利' : ({convergence:'收敛且净盈利', convergence_and_profit:'收敛且净盈利', profit:'达到净盈利', convergence_or_profit:'收敛或盈利（净收益达标）', disabled:'仅保护'})[value('close-mode')] || '收敛且净盈利';
        const usesConvergence = ['convergence', 'convergence_and_profit', 'convergence_or_profit'].includes(value('close-mode'));
        const remainingBasis = true;
        const target = channel ? (value('channel-exit-mode')==='opposite' ? '冻结对侧边界，结束后核验反向机会' : `向冻结中心回归 ${numberText('channel-reversion','60')}%`) : ({center:'历史中心', zero:'回到 0', either:'历史中心或 0'})[value('execution-close-target')] || '历史中心';
        const convergence = (usesConvergence || channel)
            ? (remainingBasis ? `回归 ${target} · 容差 ${numberText('close-convergence-bps')} bps` : `至少收窄 ${numberText('close-convergence-bps')} bps`)
            : '';
        const maxSpread = value('max-spread') === '' ? '不限' : `${value('max-spread')} bps`;
        const profit = `${numberText('close-min-profit-usdc')} USDC / ${numberText('close-min-profit-bps')} bps`;
        const hold = numberText('close-max-hold-minutes');
        const maxPositions = numberText('max-positions');
        const maxTotal = value('max-total-position-notional') === '' || Number(value('max-total-position-notional')) <= 0
            ? '不限' : `${value('max-total-position-notional')} USDC`;
        const dirty = strategyDirty ? ' · 有未保存修改' : '';
        const pendingGuard = latestOperations?.execution?.running
            && Number(latestOperations.execution.position_monitor?.profit_exit_guard_version||0) < 2
            ? ' · 新净盈利门槛待实盘重启加载' : '';
        const pendingEntryModel = latestOperations?.execution?.running
            && Number(latestOperations.execution.position_monitor?.entry_profit_model_version||0) < 2
            ? ' · 开仓成本新逻辑待实盘重启加载' : '';
        const positionMode = value('position-mode') === 'spread_grid' ? `价差网格 · 每格 ${numberText('grid-step')}% · 最多 ${numberText('grid-max-layers')} 层 · 每层 ${numberText('grid-layer-notional')} / 总额 ${numberText('grid-total-notional')} USDC` : '单开';
        if (summary) summary.textContent = `${positionMode} · ${target} · ${closeMode}${pendingGuard}${pendingEntryModel}${dirty}`;
        if (detail) detail.textContent = `${positionMode} · 开平仓共用 ${target}，开仓冻结 · 预计净回归空间：最低 ${numberText('min-spread')} + 首腿前预留 ${numberText('execution-safety-margin')} bps，上限 ${maxSpread} · 已扣完整开平仓费用、真实盘口冲击、预计平仓变价预算及资金费 ${get('execution-funding-mode')?.value==='dynamic'?'动态预测 + 额外预留 '+numberText('execution-funding-extra')+' bps，预计 '+numberText('execution-funding-hold')+' 分钟；兜底':'固定预算'} ${numberText('execution-funding-reserve')} bps · 首腿成交后补齐底线 ${numberText('min-spread')} bps，并满足最低盈利金额及收益率 · 开仓固定变价不重复扣，入场预留覆盖；每腿下单保护 / 预计平仓基础变价预算 ${numberText('execution-price-movement','2')} bps；历史滑点仅供观察，不额外加罚 · 补齐提交窗口 ${numberText('hedge-max-delay','5')} 秒 · 连续回补 ${numberText('route-rollback-limit','2')} 次暂停 ${numberText('route-rollback-cooldown','15')} 分钟 · ${closeMode}${convergence ? ` · ${convergence}` : ''} · 最低实际盈利 ${profit} · 成本预留 ${numberText('close-cost-reserve-usdc')} USDC · 持仓限制 ${maxPositions} 组 / ${maxTotal} 总额 · 最长持仓 ${hold} 分钟${dirty ? '；保存后数量与金额用于新开仓，其他设置待全部平仓' : '；已保存'}`;
    };
    const syncExecutionScope = () => {
        const selection = executionSelection();
        const all = selection.mode === 'all';
        const once = get('execution-once');
        if (once) once.textContent = all ? '单次验证（全品种等待机会）' : '实盘单次验证';
        const hint = get('execution-auto-state');
        if (hint && latestOperations?.execution?.running) {
            const execution = latestOperations.execution;
            const liveScope = runtimeScope(execution);
            const symbols = liveScope?.symbols || execution.symbols || [];
            const current = new Set(symbols);
            const samePair = !platformScope(execution).differs;
            const missing = all && samePair ? selection.symbols.filter(symbol => !current.has(symbol)) : [];
            hint.textContent = (liveScope?.version===1?'后续新仓实际范围：':'后台执行范围（启动时固定）：')+
                venueListText(liveScope?.entry_venues || execution.venues)+'；品种：'+
                (symbols.length ? symbols.length<=4 ? symbols.length+' 个（'+symbols.join('、')+'）' : symbols.length+' 个' : execution.symbol || '待核对')+
                '。后台持续复核此范围内的行情与历史筛选。'+
                (liveScope?.version===1 ? ' 参与平台调整在后台核验后应用；旧仓保留原平台。' :
                missing.length ? ' 当前共同品种范围另有 '+missing.length+' 个未纳入本次运行；下次重新检查并启动后使用完整清单。' : ' 上方修改用于下一次启动。');
            hint.title='本次运行品种：'+symbols.join('、');
        } else if (hint) hint.textContent = all
            ? '全品种执行范围：共同品种 '+selection.symbols.length+' 个（已扣排除清单）；启动时固定完整清单，后台持续筛选，不受页面当前显示行数限制'
            : '执行范围跟随上方“指定品种”：只等待当前选中品种的合格机会';
    };
    const quoteRouteText = d => {
        const venues=latestOperations?.execution?.venues || [];
        const names={lighter:'Lighter 主网',lighter_rh:'Lighter RH',arcus:'Arcus',standx:'StandX',variational:'Variational'};
        const first=names[venues[0]]||'第一平台', second=names[venues[1]]||'第二平台';
        return d.direction==='sell_variational'?'买 '+second+' / 卖 '+first:'买 '+first+' / 卖 '+second;
    };
    const executionEventText = event => {
        if(!event)return '';
        const d=event.detail||{};
        const labels={
            wait_once_started:'单次验证已启动', wait_once_skipped:'单次验证等待中：本轮报价未达标',
            stage_completed:'阶段完成', stage_failed:'阶段失败', blocked:'已拦截',
            quote_evaluation:'同数量报价复核',
            planned:'已发现达标机会，进入提交前检查', entry_receipt:'入场回执',
            hedge_decision:'首腿成交后对冲评估', route_rollback_recorded:'已记录路线回补',
            route_rollback_paused:'该路线连续回补 · 暂停新开仓',
            accounting_pending:'首腿已成交 · 费用待核对', reconcile_required:'需人工核对 · 已暂停新开仓',
            hedged:'双腿配对完成', requote_retry:'网页平台报价刷新，已重新核算一次', wait_once_stopped:'等待已停止',
            close_batch_started:'并发发送平仓', close_residual_retry:'已核对残余仓位 · 有限重试',
            close_retry_blocked:'平仓重试受阻 · 需核对', close_receipt_unknown:'平仓回执未知 · 需核对',
            execution_stop_completed:'全局实盘已停止',
            execution_stop_requires_reconcile:'进程已停止 · 仓位待核对',
            single_attempt_started:'单次执行开始', single_attempt_skipped:'单次执行跳过',
            single_attempt_stopped:'单次执行停止', candidate_fallback:'候选失败，已切换下一机会',
            position_resumed:'原仓位已接管 · 继续监控', target_reached:'已达到目标累计金额', entry_cycle:'完成一轮入场'
        };
        let text=labels[event.event]||event.event;
        if (d.preparation) text='候选并行只读核验 · '+text;
        if(d.symbol)text+=' · '+d.symbol;
        if(d.direction)text+=' · '+quoteRouteText(d);
        if(d.stage)text+=' · '+d.stage;
        if(event.event==='quote_evaluation'){
            if(Number.isFinite(d.discovery_gross_bps))text+=' · 初筛参考毛价差 '+d.discovery_gross_bps.toFixed(2)+' bps';
            if(Number.isFinite(d.gross_edge_bps))text+=' · 报价毛价差 '+d.gross_edge_bps.toFixed(2)+' bps';
            if(Number.isFinite(d.net_edge_bps))text+=(d.edge_basis==='target_return'?' · 预计净回归空间 ':' · 保护后净价差 ')+d.net_edge_bps.toFixed(2)+' bps / 门槛 '+(d.min_edge_bps??'--')+' bps';
        }
        if(d.reason)text+=' · '+d.reason;
        if(d.venue){
            const venueNames={lighter:'Lighter 主网',lighter_rh:'Lighter RH · Robinhood Chain',standx:'StandX',variational:'Variational',arcus:'Arcus'};
            text+=' · 平台 '+(venueNames[d.venue]||d.venue);
        }
        if(d.status)text+=' · 状态 '+d.status;
        if(d.filled_size!=null)text+=' · 成交 '+d.filled_size;
        return text;
    };
    const compactStatusText = value => {
        const parts = String(value || '').split(/[；;]/).map(item => item.trim()).filter(Boolean);
        if (!parts.length) return '';
        const text = parts.slice(0, 2).join(' · ');
        return parts.length > 2 || text.length > 72 ? text.slice(0, 72) + '…' : text;
    };
    const executionStateLabel = value => ({starting:'启动中',running:'运行中',finished:'已结束',failed:'已失败',idle:'未启动'})[value] || value || '未知';
    const historicalExecutionEvent = (execution, event) => {
        if (execution?.running || !event?.timestamp) return false;
        // An unresolved fill is still an active safety incident even after
        // ten minutes. Keep it visible until the pair is reconciled; hiding
        // it as ordinary history could invite a duplicate order.
        const eventName = String(event?.event || '').trim();
        if (['accounting_pending','reconcile_required'].includes(eventName)) return false;
        const at = Date.parse(event.timestamp);
        if (!Number.isFinite(at)) return false;
        // A stopped child can leave its last event in the persistent journal.
        // Do not present that old rejection as the result of the current page
        // session or of a fresh read-only check.
        return Date.now() - at > 10 * 60 * 1000;
    };
    function entryContinuationText(execution) {
        const reason=execution?.entry_scan?.capacity_block_reason;
        if(reason)return reason+'；已有仓位继续监控。';
        if(execution?.close_only)return '仅管理已有仓位，不开新仓。';
        if(execution?.position_policy?.mode==='spread_grid')return '同品种继续按网格条件和持仓上限判断加仓。';
        if(execution?.single_position_cycle===false && execution?.mode==='continuous_monitor')
            return '按并发上限继续筛选其他品种，达标后依次开下一组。';
        return '等待当前持仓平仓并核对后，再寻找下一单。';
    }
    function renderExecutionBanner(execution) {
        const root=get('execution-live-banner');
        if(!root)return;
        const title=get('execution-live-title'), detail=get('execution-live-detail'), time=get('execution-live-time');
        const event=execution?.latest_event, d=event?.detail||{};
        const historical = historicalExecutionEvent(execution, event);
        let conciseMessage='';
        let tone='idle', heading='实盘未启动', message='在执行区完成单次验证或开启全局实盘后，这里会显示是否下单以及当前执行阶段。';
        if (!execution?.running && execution?.stop_completed) {
            tone='done'; heading='全局实盘已停止';
            message=execution.note||'工作进程已退出，停止前已有仓位已确认结束。重新启动前请核对账户并解除停止。';
        } else if (!execution?.running && execution?.tracked_position_count>0) {
            tone='rejected'; heading='已有成交持仓 · 自动监控已停止';
            message=execution.note||'工作进程已退出，原持仓仍需核对；核对通过后恢复监控。';
        } else if (!execution?.running && execution?.stop_requires_reconcile && execution?.stop_requested) {
            tone='waiting'; heading='全局实盘已停止 · 仓位待核对';
            message='工作进程已退出，原仓位和停止保护已保留；不代表已平仓。点击“核对原仓位（只读）”，核对通过后重新只读检查，再手动开启实盘接管。';
        } else if (execution?.running && execution.position_monitor?.read_only &&
                execution.position_monitor.stop_reason && !execution.position_monitor.heartbeat_stale) {
            tone='rejected'; heading='自动下单已暂停 · 持仓只读监控中';
            message=execution.position_monitor.stop_reason+'；行情与账户继续更新，自动开平仓已锁定。请停止本次执行，核对原订单后恢复监控。';
        } else if (execution?.running && execution?.recovery_blocked) {
            tone='waiting'; heading='原仓位待接管 · 需重新核对';
            message='进程正在运行，但旧仓位仍为待核对状态，不能视为已接管。请停止旧进程后，重新只读检查并开启实盘。';
        } else if (execution?.running && execution?.close_only && !execution?.stop_requested) {
            tone='waiting';
            heading=execution.tracked_position_count===0
                ? '只平仓监控中 · 当前无待管理仓位' : '只平仓监控中 · 不开新仓';
            message='本次运行模式为“仅管理已有仓位”，不会寻找入场机会或提交新开仓。'+
                (execution.managed_positions?.length?'已接管 '+execution.managed_positions.map(p=>p.symbol).join('、')+'；继续按平仓规则监控。':'')+
                (event?'最新进度：'+executionEventText(event)+'。':'')+
                '要改为自动开仓与平仓，先结束本次运行，再选择模式、重新只读检查并手动启动。';
        } else if (execution?.running && !execution?.stop_requested &&
                execution.position_monitor?.entry_paused && !execution.position_monitor.heartbeat_stale) {
            tone='waiting'; heading='新开仓暂停 · 持仓核验受阻';
            const positions=execution.position_monitor.positions||[];
            const pending=positions.find(p=>p.account_status!=='ready');
            const quoteBlocked=!pending && positions.find(p=>/报价|行情|盘口/.test(p.error||'') ||
                Object.values(p.legs||{}).some(leg=>leg.error));
            if (quoteBlocked) {
                heading='新开仓暂停 · 持仓行情受阻';
                const venueNames={lighter:'Lighter 主网',lighter_rh:'Lighter RH',arcus:'Arcus',standx:'StandX',variational:'Variational'};
                const errors=Object.values(quoteBlocked.legs||{}).filter(leg=>leg.error)
                    .map(leg=>(venueNames[leg.venue]||leg.venue||'平台')+'：'+leg.error);
                message=quoteBlocked.symbol+' · '+(errors.join('；')||quoteBlocked.error)+
                    '；后台重试中，行情及持仓核验恢复后自动继续扫描。';
                if (message.includes('行情后台校时中')) {
                    heading='新开仓暂停 · 后台校时中';
                } else if (message.includes('时间超前')) {
                    const calibrated=Object.values(quoteBlocked.legs||{}).some(leg=>leg.connection?.quote_clock?.ready);
                    message+=calibrated?'内部校时已就绪，等待平台新报价。':'后台正在校时，无需调整本机时间。';
                }
            } else {
                const venueNames={lighter:'Lighter 主网',lighter_rh:'Lighter RH',arcus:'Arcus',standx:'StandX',variational:'Variational'};
                const affected=[...new Set(positions.filter(p=>p.account_status!=='ready').map(p=>p.symbol))];
                const recovery=Object.values(pending?.legs||{}).filter(leg=>leg.account?.status==='pending').map(leg=>{
                    const account=leg.account;
                    return (venueNames[leg.venue]||leg.venue)+'：'+(account.error||'等待账户核验')+
                        (Number.isFinite(account.blocked_for_seconds)?'，已持续 '+Math.floor(account.blocked_for_seconds)+' 秒':'')+
                        (account.last_success_at?'，账户最近成功 '+date(account.last_success_at):'，尚无有效账户快照')+
                        (account.reading?'，正在重试':Number.isFinite(account.retry_in_seconds)?'，'+Math.ceil(account.retry_in_seconds)+' 秒后重试':'');
                });
                message=(execution.position_monitor.entry_pause_reason ||
                    (pending?.symbol ? pending.symbol+' 账户核验受阻，后台重试中' : '持仓监控待核验，后台重试中'))+
                    '；核验恢复后自动继续扫描。'+(affected.length>1?'受影响持仓：'+affected.join('、')+'。':'')+
                    (recovery.length?'原因：'+recovery.join('；'):pending?.account_errors?.length?'原因：'+pending.account_errors.join('；'):'');
            }
        } else if (execution?.running && execution?.managed_positions?.length && !execution?.stop_requested) {
            const checkedAfterEvent=execution.position_monitor?.positions?.length && !execution.position_monitor.heartbeat_stale &&
                execution.position_monitor.positions.every(p=>p.account_status==='ready' && Date.parse(p.last_success_at||'')>Date.parse(event?.timestamp||''));
            const blocked=event?.event==='position_reconcile_failed' && !checkedAfterEvent;
            tone=blocked?'waiting':'done'; heading=blocked?'原仓位已接管 · 账户复核受阻':'原仓位已接管 · 持仓监控中';
            message=blocked ? '账户复核暂不可用；查看持仓卡片与运行明细。' : '正在监控 '+execution.managed_positions.map(p=>p.symbol).join('、')+'，沿用原成交记录；'+entryContinuationText(execution);
        } else if(execution?.running && execution?.stop_requested && execution?.mode==='continuous_monitor') {
            tone='waiting'; heading=drainStatus(execution).label;
            message=drainStatus(execution).detail;
        } else if (historical) {
            heading='暂无当前执行结果';
            message='上次历史结果：'+(executionEventText(event)||'未确认成交')+'；当前只读检查和新执行需要单独查看。';
        } else if((event?.event==='hedged' || event?.event==='entry_cycle') && execution?.running && ['once','wait_once'].includes(execution?.mode)) {
            tone='waiting'; heading='单次验证中 · 等待平仓';
            message='两腿成交回执已确认；正在等待价差收敛或最长持仓保护，平仓后本次验证才结束。';
        } else if(event?.event==='hedged' || event?.event==='entry_cycle') {
            tone='done'; heading='已下单 · 双腿持仓监控中';
            message='两腿成交回执已确认，订单已写入执行历史；'+
                (execution?.running ? entryContinuationText(execution) : '查看执行历史核对成交。');
        } else if(execution?.running){
            const waitingName = execution.mode === 'continuous_monitor' ? '全局实盘等待中' : '单次验证等待中';
            const lastRejection=execution?.last_rejection;
            const lastQuote=execution?.rejection_quote_evaluation;
            const sameAttempt=lastQuote && lastRejection?.detail?.execution_id &&
                lastQuote.detail?.execution_id===lastRejection.detail.execution_id &&
                lastQuote.detail?.symbol===lastRejection.detail.symbol;
            const q=lastQuote?.detail || {};
            const quoteSummary=sameAttempt && Number.isFinite(q.net_edge_bps)
                ? q.symbol+' · '+quoteRouteText(q)+(q.edge_basis==='target_return'?' · 预计净回归空间 ':' · 保护后净价差 ')+q.net_edge_bps.toFixed(2)+' / 门槛 '+(q.min_edge_bps??'--')+' bps（该次检查）' : '';
            const referenceBlocker=execution?.reference_blocker;
            const blockerAt=Date.parse(referenceBlocker?.timestamp || '');
            const referenceCooldown=String(referenceBlocker?.detail?.reason || '').match(/HTTP 429；暂停 REST 请求 (\d+) 秒/);
            const referenceCooldownActive=!referenceCooldown || Date.now()-blockerAt<Number(referenceCooldown[1])*1000;
            const blockedReference=referenceBlocker && Number.isFinite(blockerAt) &&
                Date.now()-blockerAt >= 0 && Date.now()-blockerAt <= 30000 &&
                referenceCooldownActive &&
                ['stage_completed','stage_failed','wait_once_skipped'].includes(event?.event);
            const resolvedReference=lastRejection?.event==='stage_failed' &&
                String(lastRejection.detail?.stage || '').endsWith(' 参考价格') && !referenceBlocker;
            const waitingDetail=lastRejection && !resolvedReference ? '最近拦截（'+date(lastRejection.timestamp)+'）：'+executionEventText(lastRejection)+
                (sameAttempt ? '；该次检查较优方向：'+executionEventText(lastQuote) : '') : '';
            const backoffUntil=Date.parse(execution.entry_scan_backoff_until || '');
            const backoffSeconds=Number.isFinite(backoffUntil) ? Math.max(0,Math.ceil((backoffUntil-Date.now())/1000)) : 0;
            const previewSkip=execution.entry_scan?.last_preview_skip;
            const previewAge=Date.now()-Date.parse(previewSkip?.updated_at || '');
            const capacityReason=execution.entry_scan?.capacity_block_reason;
            const entryBusy=['planned','entry_submit_intent','entry_receipt'].includes(event?.event) ||
                String(event?.event||'').startsWith('stage_') && /提交|签名|回执|对冲|入场/.test(d.stage||'');
            if(capacityReason && !entryBusy){
                tone='waiting'; heading='新开仓等待 · '+capacityReason;
                message=capacityReason+'；已有仓位继续按平仓条件监控，限制解除后重新筛选机会。';
            }else if(backoffSeconds>0){
                tone='waiting'; heading='平台 REST 限流等待 · 新开仓扫描暂停';
                message='约 '+backoffSeconds+' 秒后重新检查；遵守平台冷却时间，已有仓位继续监控。'+
                    (waitingDetail ? '上次记录：'+executionEventText(lastRejection)+'。' : '');
            }else if(previewSkip && previewAge>=0 && previewAge<=10000 && !blockedReference &&
                (!event || ['blocked','stage_failed'].includes(event.event) ||
                    event.event==='stage_completed' && String(d.stage||'').endsWith(' 参考价格'))){
                tone='waiting'; heading=waitingName+' · 盘口预判未达标';
                message='最近预判：'+previewSkip.symbol+' · '+previewSkip.route+
                    ' · 乐观整轮净收益 '+Number(previewSkip.optimistic_net_bps).toFixed(2)+
                    ' / 门槛 '+Number(previewSkip.minimum_bps).toFixed(2)+' bps。'+
                    '预判忽略盘口冲击、价格变动和资金费预留；仍不达标时跳过深度请求，继续扫描其他机会。';
            }else if(blockedReference){
                const failure=referenceBlocker.detail || {};
                const localWait=/REST 共享节流|参考价探测冷却/.test(failure.reason || '');
                const exchangeWait=/HTTP 429/.test(failure.reason || '');
                tone=localWait||exchangeWait ? 'waiting' : 'rejected';
                heading=(localWait ? '实盘扫描等待' : exchangeWait ? '平台 REST 限流等待' : '实盘报价受阻')+' · '+(failure.symbol ? failure.symbol+' · ' : '')+(failure.stage || '参考价格');
                message=(failure.reason || '参考报价读取失败')+(localWait
                    ? '；此品种暂缓 REST 探测；其他品种继续扫描，收到有效长连接报价后自动恢复。'
                    : exchangeWait ? '；遵守平台冷却时间，暂停新开仓扫描；已有仓位继续监控。'
                    : '；该品种在此平台组合的报价不完整，跳过该组合；其他可用品种和平台组合继续扫描。');
            }else if(event?.event==='wait_once_skipped' || event?.event==='blocked'){
                tone='waiting'; heading=waitingName+' · 本轮未达标';
                message=(waitingDetail||executionEventText(event))+'；继续等待下一轮，不会立即发单。';
                conciseMessage=quoteSummary;
            }else if(event?.event==='stage_completed'){
                tone='waiting'; heading=waitingName+' · 正在核验双平台报价';
                message=(waitingDetail||'已完成 '+(d.stage||'一个检查阶段')+'；仍需双腿报价和提交前检查均通过。')+'；继续等待下一轮新鲜报价。';
                conciseMessage=quoteSummary;
            }else if(event?.event==='stage_failed'){
                tone='waiting'; heading=waitingName+' · 本轮检查未通过';
                message=(waitingDetail||executionEventText(event))+'；未提交订单，等待下一轮新鲜报价。';
                conciseMessage=quoteSummary;
            }else
            if(event?.event==='planned'){
                tone='checking'; heading='已达到入场条件 · 尚未下单';
                message='正在进行账户、RFQ 和双腿提交前检查；检查不通过会安全拒绝。';
            }else if(event?.event==='stage_completed' || event?.event==='entry_receipt'){
                tone='checking'; heading='提交处理中 · 尚未确认成交';
                message=executionEventText(event);
            }else{
                tone='waiting'; heading=waitingName+' · 尚未下单';
                message=(waitingDetail||'正在读取双平台参考报价；初筛通过后，按实际数量复核买卖价、费用及价格保护，净价差达标才继续下单检查。')+'；不会重复提交。';
            }
        }else if(event?.event==='accounting_pending' || event?.event==='reconcile_required'){
            tone='rejected'; heading='首腿已成交 · 第二腿未确认';
            message=executionEventText(event)+'；费用回执未确认，第二腿没有继续提交；已暂停新开仓，请核对两边账户、活动订单和持仓后再处理。';
        }else if(event?.event==='entry_receipt' && Number(d.filled_size||0)>0 && d.status!=='rejected'){
            tone='done'; heading='已收到入场回执'; message=executionEventText(event);
        }else if(event?.event==='entry_receipt' || event?.event==='blocked' || event?.event==='stage_failed' || d.status==='rejected'){
            tone='rejected'; heading='已结束 · 未成交'; message=executionEventText(event)||'本轮没有确认成交。';
        }else if(execution?.state==='failed'){
            tone='rejected'; heading='执行失败 · 未确认成交'; message=execution.note||'请查看执行历史和账户状态。';
        }else if(execution?.state==='finished'){
            tone='done'; heading='执行已结束'; message=execution.note||'请以执行历史和账户核对结果为准。';
        }else if(execution?.state==='starting'){
            tone='checking'; heading='正在启动执行'; message='执行进程已启动，等待第一条阶段事件。';
        }
        if (platformScope(execution).differs) {
            tone='waiting';
            conciseMessage='';
            if (!execution.recovery_blocked) heading=execution.stop_requested ? '旧组合：'+drainStatus(execution).label : '平台已改选 · 实盘仍运行旧组合';
            message='正在执行 '+venueListText(platformScope(execution).active)+'；当前选择尚未生效。'+message;
        }
        renderPlatformScope(execution);
        globalThis.updateDeskExecution?.(execution);
        root.className='execution-live-banner '+tone;
        title.textContent=heading;
        detail.textContent=conciseMessage || compactStatusText(message) || message;
        detail.title=message;
        const monitorAt=execution?.running && execution?.position_monitor?.updated_at;
        const scanAt=execution?.position_monitor?.entry_scan?.cached_scan?.updated_at;
        time.textContent=monitorAt?'监控更新 '+date(monitorAt)+(scanAt?' · 最近扫描 '+date(scanAt):''):event?.timestamp?'最近更新 '+date(event.timestamp):execution?.finished_at?'结束于 '+date(execution.finished_at):'等待状态';
    }
    const invalidate = (reason='开关未解锁：请先点击“只读检查”。') => {
        checkedScopeKey=null;
        checkedExecutionPair=null;
        checkedUntil=0;armed=false;setDisabled('execution-once', true);setDisabled('execution-monitor', true);
        if (/检查请求失败|未通过|失败/.test(reason)) checkState='failed';
        else if (/过期/.test(reason)) checkState='expired';
        else if (checkState !== 'checking') checkState='idle';
        const liveArm = get('live-arm');
        const canStop = running && latestOperations?.execution?.mode === 'continuous_monitor';
        if (liveArm) {
            liveArm.setAttribute('aria-pressed',String(running));
            liveArm.disabled=canStop ? checking || stopRequested : true;
            liveArm.title=canStop ? (stopRequested ? drainStatus(latestOperations.execution).detail : '停止 '+venueListText(latestOperations.execution.venues)+' 新开仓；已有仓位继续按规则平仓，确认结束后退出') : reason;
        }
        liveLabel(canStop ? (stopRequested ? drainStatus(latestOperations.execution).label : platformScope(latestOperations.execution).differs ? '停止旧组合新开仓' : latestOperations.execution.close_only ? '结束只平仓监控' : '停止新开仓') : running ? '单次验证：运行中' : '全局实盘：关闭');
        armStatus(canStop && stopRequested ? drainStatus(latestOperations.execution).detail : reason);
    };
    function updateArmStatus() {
        if(!checkedUntil)return;
        const seconds=Math.ceil((checkedUntil-Date.now())/1000);
        if(seconds<=0){invalidate('检查已过期，开关已锁定：请重新点击“只读检查”。');return;}
        armStatus((executionPolicy().close_only?'检查通过，可以启动只平仓监控':armed?'全局实盘正在运行':'检查通过，可以开启全局实盘')+' · 剩余 '+seconds+' 秒；过期需重新检查。');
    }
    async function resumeEntries() {
        if (checking || strategyDirty) return;
        const control = latestOperations?.execution?.resume_control;
        if (!control?.available) return;
        checking = true;
        renderResumeControl();
        setDisabled('live-arm', true);
        const result = get('execution-check-result');
        if (result) result.textContent = '正在核验原进程和已有仓位，恢复后按已保存上限等待新机会…';
        const abort = new AbortController(), timer = setTimeout(()=>abort.abort(),10000);
        try {
            const response = await fetch('/api/execution/resume', {
                method:'POST', signal:abort.signal,
                headers:{'Content-Type':'application/json','X-Local-Token':document.querySelector('meta[name="local-token"]').content},
                body:JSON.stringify({pid:control.pid,started_at:control.started_at,stop_generation:control.stop_generation})
            });
            const data = await readJsonResponse(response, '恢复新开仓');
            if (!data.success) throw new Error(data.message || '恢复请求未通过');
            if (result) result.textContent = data.message;
        } catch (error) {
            if (result) result.textContent = error.name === 'AbortError'
                ? '恢复请求超时，请等待状态更新后确认是否已恢复。' : '恢复未完成：'+error.message;
        } finally {
            clearTimeout(timer);
            checking = false;
            renderResumeControl();
            await refresh();
        }
    }

    async function executionAction(action) {
        if (checking) return;
        if (running && ['check','once','wait-once','monitor'].includes(action)) {
            get('execution-check-result').textContent='正在执行 '+venueListText(latestOperations?.execution?.venues)+
                '；先停止旧组合新开仓，确认空仓且进程退出后再检查新组合。';
            return;
        }
        if (['once','wait-once'].includes(action) && executionPolicy().close_only) {
            get('execution-check-result').textContent='仅管理已有仓位模式不执行开仓验证；请检查后启动只平仓监控。未发送订单。';
            return;
        }
        if (['once','wait-once','monitor'].includes(action) && checkedScopeKey !== scopeKey()) {
            invalidate('参与平台、品种或执行条件已修改，请重新执行只读检查。');
            return;
        }
        if (action === 'check' && strategyDirty) {
            const result = get('execution-check-result');
            if (result) result.textContent = '策略有未保存修改，请先点击“保存策略”；未发送订单。';
            return;
        }
        const selection = executionSelection(), requestScopeKey = scopeKey();
        const autoWait = action==='wait-once' || (action==='once' && selection.mode==='all');
        if (action==='monitor' && (!armed || Date.now()>checkedUntil)) {invalidate();get('execution-check-result').textContent='全局实盘开关未启用或只读检查已过期；未发送订单。';return;}
        if (action==='wait-once' && Date.now()>checkedUntil) {invalidate();get('execution-check-result').textContent='只读检查已过期；请重新检查，未发送订单。';return;}
        if (action==='once' && Date.now()>checkedUntil) {invalidate();get('execution-check-result').textContent='只读检查已过期；请重新检查，未发送订单。';return;}
        checking=true;
        if (action==='check') checkState='checking';
        const continuous=action==='monitor' && armed;
        if(continuous){setDisabled('live-arm',true);liveLabel('全局实盘：启动中');armStatus(executionPolicy().close_only?'正在启动只平仓监控，不会开新仓。':'全局实盘正在启动，等待下一次合格机会；不会立即发单。');}
        else if(autoWait){setDisabled('live-arm',true);liveLabel('全局实盘：关闭');armStatus('单次验证正在启动；只读检查已通过，等待合格机会后完成一轮开仓→平仓。');}
        else invalidate('正在'+(action==='prepare'?'准备会话':action==='check'?'检查':'处理请求')+'，开关暂不可用。');
        setDisabled('execution-check', true);
        setDisabled('execution-prepare', true);setDisabled('execution-preview', true);setDisabled('execution-reset-stop', true);setDisabled('execution-monitor', true);
        setDisabled('execution-once', true);
        setDisabled('execution-temporary-cap', true);setDisabled('execution-cap', true);setDisabled('execution-target', true);setDisabled('execution-fallback', true);setDisabled('execution-trade-mode', true);
        setDisabled('execution-entry-mode', true);setDisabled('execution-entry-quantile', true);setDisabled('execution-entry-min-samples', true);setDisabled('execution-close-target', true);setDisabled('save-execution-strategy', true);
        strategyControlIds.forEach(id=>setDisabled(id,true));
        get('execution-check-result').textContent=action==='preview'?'正在复测同数量报价、深度和净价差（最多 45 秒）；未调用下单…':action==='reset-stop'?'正在只读核对两平台账户并解除停止（最多 45 秒）；不会下单…':action==='prepare'?'正在准备程序连接的浏览器表单、核对报价与账户（最多 45 秒），不会点击交易提交…':action==='check'?'正在只读核对平台最低金额和执行条件…':action==='stop'?'正在请求停止新开仓；已有仓位继续按规则监控平仓…':action==='monitor'?(executionPolicy().close_only?'正在启动只平仓监控，不会开新仓…':'正在启动持续监控；只有预计净回归空间达标且通过复核才会提交双腿…'):autoWait?'正在等待下一次预计净回归空间达标的机会；达标后完成一轮开仓→平仓后结束…':action==='once'?'正在执行一次实盘验证；成交后继续等待平仓，完成一轮后结束…':'正在处理，请勿重复操作…';
        try {
            const selected=globalThis.getSelectedVenues?.()||[];
            const selectedPair = selectedExecutionPair(selected);
            if(['check','once','wait-once','monitor'].includes(action) && selected.length<2){
                checking=false;
                const message='实盘执行至少需要两个平台；当前已选择 '+selected.length+' 个。行情扫描仍可多选平台。';
                invalidate(message);get('execution-check-result').textContent=message+' 未发送订单。';
                return;
            }
            // Resetting a safety stop is account reconciliation and must not
            // depend on a currently visible opportunity. Reuse the last
            // recorded execution pair when clearing its stop, even if the
            // scanner has already been changed to a different pair.
            const recordedPair = latestOperations?.execution?.execution_pair ||
                (Array.isArray(latestOperations?.execution?.venues) && latestOperations.execution.venues.length===2
                    ? latestOperations.execution.venues : null);
            const resetPair = action==='reset-stop' && (stopped || latestOperations?.execution?.tracked_position_count>0) &&
                Array.isArray(recordedPair) && recordedPair.length===2 ? recordedPair : null;
            const bestPair=action==='reset-stop' ? (resetPair || (selected.length===2 ? selected : null)) : selectedPair;
            if(action==='reset-stop' && (!bestPair || bestPair.length!==2)){
                checking=false;
                const message='解除停止需要上次执行的两个平台；当前没有可复用的执行组合，请暂时只选择上次执行的两个平台后重试。';
                invalidate(message);get('execution-check-result').textContent=message+' 未发送订单。';
                return;
            }
            const executionVenues=(['once','wait-once','monitor'].includes(action) && checkedExecutionPair)
                ? checkedExecutionPair.slice() : bestPair || selected;
            const signedPair=['lighter','lighter_rh','standx','arcus'];
            const pair=executionVenues.length===2 && executionVenues.every(venue=>signedPair.includes(venue)) ? executionVenues : undefined;
            const legacyPair=executionVenues.length===2 && executionVenues.includes('variational') && executionVenues.includes('lighter');
            const payload=action==='stop'?{}:action==='reset-stop'?{
                venues: executionVenues
            }:{
                symbol: selection.mode === 'all' ? 'ALL' : selection.symbols[0],
                symbols: selection.symbols,
                mode: selection.mode,
                max_notional:Number(get('execution-cap').value),
                ...executionPolicy(),
                scan_venues:[...new Set(selected)],
                ...(pair?{venues:pair}:legacyPair?{}:{venues:executionVenues})
            };
            const r=await fetch('/api/execution/'+action,{method:'POST',headers:{'Content-Type':'application/json','X-Local-Token':document.querySelector('meta[name="local-token"]').content},body:JSON.stringify(payload)});
            const data=await readJsonResponse(r,'执行接口');
            if(data.error)throw new Error(data.error);
            const budget=data.budget;
            const capHint=get('execution-cap-hint');
            if (capHint && budget) {
                if (budget.estimated_minimum_notional != null) {
                    capHint.textContent=`Lighter 最低有效开仓约 ${Number(budget.estimated_minimum_notional).toFixed(2)} USDC；当前本次上限 ${budget.cap_usdc} USDC${Number(get('execution-cap').value) < Number(budget.estimated_minimum_notional) ? ' · 低于最低开仓，检查不会通过' : ' · 可执行上限，最终按实时盘口自动缩量'}`;
                } else if (budget.effective_minimum_notional != null) {
                    const names = (budget.entries || []).map(item => minimumVenueNames[item.venue] || item.venue).join(' / ');
                    capHint.textContent=`${names || '选中平台'}最低有效开仓约 ${Number(budget.effective_minimum_notional).toFixed(2)} USDC；当前本次上限 ${budget.cap_usdc} USDC${Number(get('execution-cap').value) < Number(budget.effective_minimum_notional) ? ' · 低于最低门槛，检查不会通过' : ' · 可执行上限，最终按实时盘口自动缩量'}`;
                }
            }
            const budgetText = budget?.estimated_minimum_notional != null
                ? 'Lighter 最低开仓 ' + budget.minimum_quote_usdc + ' USDC，当前数量步长取整后约 ' + Number(budget.estimated_minimum_notional).toFixed(4) + ' USDC；当前本次执行上限 ' + budget.cap_usdc + ' USDC。'
                : budget?.effective_minimum_notional != null
                    ? '选中平台两腿有效最低开仓约 ' + Number(budget.effective_minimum_notional).toFixed(4) + ' USDC；当前本次执行上限 ' + budget.cap_usdc + ' USDC。'
                    : '';
            const routeText = Array.isArray(data.route_candidates) && data.route_candidates.length > 1
                ? '候选路线：' + data.route_candidates.map(item =>
                    (item.venues || []).map(v => minimumVenueNames[v] || v).join(' ↔ ') +
                    (item.ready ? ' ✓' : ' · ' + ((item.blockers || []).slice(0, 1).join('；') || '未通过'))
                ).join('；') + '。'
                : '';
            get('execution-check-result').textContent=budgetText+routeText+
                (data.blockers?.length?data.blockers.join('；'):(data.message|| (data.started?(autoWait?'单次验证已启动；达到条件后完成一轮开仓→平仓。':action==='monitor'?'自动监控已启动，请查看执行历史。':'单次执行已启动，请查看执行历史。'):'只读检查通过；可点击单次验证完成一轮，或开启全局实盘持续等待。')));
            if(action==='check' && requestScopeKey !== scopeKey()) {
                invalidate('检查期间参与平台、品种或执行条件已修改；旧检查结果已失效，请重新只读检查。');
                get('execution-check-result').textContent='本次返回的是修改前组合的检查结果，未解锁实盘；请检查当前组合。';
            } else if(action==='check'&&data.ready===true){
                const checkedPair = data.venues || payload.venues || executionVenues;
                if (!Array.isArray(checkedPair) || checkedPair.length !== 2 ||
                    new Set(checkedPair).size !== 2 || checkedPair.some(venue => !selected.includes(venue)))
                    throw new Error('检查返回的执行组合不在当前授权平台范围内，请重新检查');
                checkedExecutionPair=checkedPair.slice();
                checkedScopeKey=requestScopeKey;
                checkState='passed';
                checkedUntil=Date.now()+CHECK_WINDOW_MS;setDisabled('live-arm', false);
                setDisabled('execution-once', executionPolicy().close_only);
                liveLabel(executionPolicy().close_only?'启动只平仓监控':'全局实盘：可开启');
                updateArmStatus();
                get('execution-check-result').textContent+=executionPolicy().close_only
                    ? '；本次检查模式：仅管理已有仓位，不开新仓。点击主按钮启动只平仓监控。'
                    : '；本次检查模式：自动开仓与平仓。确认金额和市场后，可单次验证或开启全局实盘。';
            } else if(action==='check'){
                checkState='failed';
                const blockers=data.blockers?.length?data.blockers.join('；'):'请查看检查结果中的阻塞原因';
                invalidate('检查已完成但未通过：'+blockers+'；未启用实盘开关。');
            } else if(action==='prepare'&&data.ready===true){
                invalidate('会话已准备；下一步点击“只读检查”，通过后才能开启全局实盘。');
            } else if(action==='reset-stop'&&data.success===true){
                stopped=false;
                invalidate(data.resume_ready ? '已核对已有持仓与两平台账户一致；可恢复监控。请重新点击“只读检查”，不会自动启用实盘。' : '停止已解除；请重新点击“只读检查”，不会自动启用实盘。');
            } else if(continuous&&data.started){
                checkedUntil=0;armed=true;setDisabled('live-arm',true);liveLabel('全局实盘：运行中');
                armStatus(executionPolicy().close_only?'只平仓监控已启动，不会开新仓。':'全局实盘已启动：持续等待合格净价差；达到条件后自动提交双腿。');
            } else if(autoWait&&data.started){
                checkedUntil=0;armed=false;setDisabled('live-arm',true);
                armStatus('单次验证已启动：等待合格机会并完成一轮开仓→平仓，不启动持续监控。');
            } else if(action==='once'&&data.started){
                invalidate('实盘单次验证已启动；成交后会继续等待平仓，完成一轮后结束。');
            } else if(autoWait){
                invalidate('单次验证未启动：请查看阻塞原因，处理后重新只读检查。');
            } else if(['once','monitor'].includes(action)) invalidate();
            else if(!autoWait) invalidate('开关未解锁：请查看下方检查结果，处理后重新执行只读检查。');
            await refresh();
        } catch(error){
            const message=error instanceof Error?error.message:String(error);
            const label=action==='check'?'检查请求失败':'执行请求失败';
            invalidate(label+'：'+message+'；开关保持锁定。');
            get('execution-check-result').textContent=label+'：'+message+'。未发送订单。';
        }
        finally{
            checking=false;
            setDisabled('execution-check', running);setDisabled('execution-prepare', false);setDisabled('execution-preview', false);
            const canReconcilePair=(globalThis.getSelectedVenues?.()||[]).length>=2;
        syncExecutionCapLimit();setDisabled('execution-target', false);setDisabled('execution-fallback', false);syncExecutionMode();
            setDisabled('execution-entry-mode', false);setDisabled('execution-entry-quantile', false);setDisabled('execution-entry-min-samples', false);setDisabled('execution-close-target', false);setDisabled('save-execution-strategy', false);setDisabled('execution-reset-stop', running||(!stopped&&!canReconcilePair));
            strategyControlIds.forEach(id=>setDisabled(id,false));
            if(checkedUntil && Date.now()<=checkedUntil && !running){setDisabled('execution-once', executionPolicy().close_only);}
            if(armed && Date.now()<=checkedUntil){setDisabled('execution-monitor', false);}
        }
    }
    const line = (root, text, className='chart-note') => {
        const node = document.createElement('p'); node.className = className;
        node.textContent = text; root.appendChild(node);
    };
    const date = value => value ? new Date(value).toLocaleString('zh-CN',{hour12:false}) : '未收到';
    const table = (root, headings, rows) => {
        root.replaceChildren();
        if (!rows.length) {line(root, '暂无记录'); return;}
        const el = document.createElement('table');
        const head = document.createElement('tr');
        headings.forEach(value => {const td=document.createElement('th');td.textContent=value;head.appendChild(td);});
        el.appendChild(head);
        rows.forEach(values => {
            const row=document.createElement('tr');
            values.forEach(value => {const td=document.createElement('td');td.textContent=value ?? '未知';row.appendChild(td);});
            el.appendChild(row);
        });
        root.appendChild(el);
    };
    async function refresh() {
        if (pending) return;
        syncStrategySummary();
        syncExecutionScope();
        pending = true;
        const control = new AbortController();
        const timer = setTimeout(()=>control.abort(),10000);
        try {
            const selectedForCheck = globalThis.getSelectedVenues?.() || [];
            const symbolForCheck = executionSelection().symbols?.[0] || '';
            const params = new URLSearchParams();
            if (selectedForCheck.length) params.set('venues', selectedForCheck.join(','));
            if (symbolForCheck) params.set('symbol', symbolForCheck);
            const response = await fetch('/api/operations'+(params.toString()?'?'+params.toString():''),{cache:'no-store',signal:control.signal});
            if (!response.ok) throw new Error('HTTP '+response.status);
            const data = await readJsonResponse(response,'诊断接口'), c = data.capture, l = data.lighter;
            latestOperations = data;
            renderEntryLimits(data.execution);
            if (typeof window !== 'undefined') window.updateOpportunityExecutionContext?.(data.execution);
            syncStrategySummary();
            syncExecutionScope();
            const observer=data.bridge.observer;
            if(observer&&!observerBusy){
                setDisabled('quote-observer-start', observer.running);
                setDisabled('quote-observer-stop', !observer.running);
                const pages=Object.values(observer.pages||{}), livePage=pages.find(page=>page.connected);
                const waiting=observer.running&&livePage&&observer.received===0&&livePage.quote_requests===0;
                get('quote-observer-status').textContent=observer.running?
                    (waiting?'页面连接正常 · 尚未触发数量报价；请在官网输入数量或刷新买/卖报价':
                    '接口监听中 · 页面 '+(observer.markets.join(' / ')||'等待连接')+' · 已收到 '+observer.received+' 次报价')+
                    (observer.error?' · '+observer.error:''):
                    '接口监听未启动；点击连接后接收已有官网页面报价。';
            }
            if (running && !data.execution.running && get('execution-temporary-cap')) get('execution-temporary-cap').checked=false;
            stopped=data.execution.stop_requested===true;stopRequested=stopped;running=data.execution.running===true;
            syncExecutionCapLimit();
            renderResumeControl(data.execution);
            renderEntryScopeControl(data.execution);
            syncExecutionMode();syncExecutionPolicy();
            renderExecutionBanner(data.execution);
            const resetStop=get('execution-reset-stop');
            const resetNeeded=stopped||data.pair_reconcile_required===true;
            setDisabled('execution-reset-stop', checking||running||!resetNeeded);
            if(resetStop){
                const recoveryNeeded=data.pair_has_open_positions===true && data.pair_resume_ready!==true;
                resetStop.textContent=recoveryNeeded?'核对原仓位（只读）':resetNeeded?'核对账户并解除停止':(data.pair_resume_ready===true||data.execution.reconciliation_state==='resume_ready')?'已核对，开启实盘后接管':'暂无停止标志';
                resetStop.title=resetNeeded
                    ? (recoveryNeeded?'只读核对已有仓位与两平台账户，匹配后才能恢复监控；不会下单':'只读核对原执行范围内的平台账户、活动订单和持仓后解除停止；不会下单')
                    :'当前没有停止标志或待核对状态；无需重复点击。';
            }
            const stopButton=get('execution-stop');
            if(stopButton){
                // Continuous monitoring has one stop control: its main button.
                stopButton.hidden=!running || data.execution.mode==='continuous_monitor';
                if(running && !stopRequested){
                    setDisabled('execution-stop',checking);
                    stopButton.textContent=platformScope(data.execution).differs ? '停止旧组合新开仓' : '停止新开仓';
                    stopButton.title='停止新开仓；已有仓位继续按规则监控平仓，确认空仓后退出。';
                }else if(running && stopRequested){
                    setDisabled('execution-stop',true);
                    stopButton.textContent=drainStatus(data.execution).label;
                    stopButton.title=drainStatus(data.execution).detail;
                }else if(resetNeeded){
                    setDisabled('execution-stop',true);
                    stopButton.textContent='需先核对账户';
                    stopButton.title='当前有停止标志或待核对持仓，先完成账户核对。';
                }else{
                    setDisabled('execution-stop',true);
                    stopButton.textContent='结束本次运行（未运行）';
                    stopButton.title='全局实盘运行后可用；主按钮运行中也会切换为停止。';
                }
            }
            const acceptance=data.manual_acceptance;
            get('manual-acceptance-result').textContent=acceptance?.manual_round_trip_observed?
                '最近手动观测（历史记录，不代表当前持仓）：'+acceptance.legs.map(x=>(x.side==='buy'?'买 ':'卖 ')+x.quantity+' ETH @ '+x.average_price).join(' → ')+
                '；'+date(acceptance.flat_snapshot_at)+' 收到完整空仓快照。毛价差收益 '+acceptance.gross_price_pnl_usdc+' USDC；'+(Number.isFinite(acceptance.realized_pnl_usdc)?'平台已确认已实现 PNL '+acceptance.realized_pnl_usdc+' USDC；':'')+'资金费另行核对，不代表自动或双平台验收完成。':'';
            const selectedVenues = globalThis.getSelectedVenues?.() || [];
            refreshMinimumHint();
            const executionPair = selectedExecutionPair(selectedVenues);
            const executionPairReady = Boolean(executionPair && executionPair.length === 2);
            const executionScopeReady = selectedVenues.length >= 2;
            setDisabled('execution-check', checking || running || !executionScopeReady);
            const quoteReady = Boolean(data.bridge.symbols?.length);
            const credentialReady = Boolean(l.verified_fresh);
            const venueScope = selectedVenues.length ? ` · 已选 ${selectedVenues.length} 个平台` : '';
            get('readiness-summary').textContent = data.execution.running ? '执行进行中 · 请勿重复提交' :
                checkState==='expired' ? '只读检查已过期 · 重新检查后才能开启实盘' :
                checkState==='checking' ? '只读检查进行中 · 不会下单' :
                checkState==='passed' && checkedUntil>Date.now() ? '只读检查已通过 · 可单次验证或开启全局实盘' :
                data.blockers?.length ? `实盘待检查 · ${data.blockers.length} 项待处理${venueScope}` :
                `配置就绪 · 需单次检查 · ${credentialReady && quoteReady ? '报价与账户条件已观察' : credentialReady ? '账户条件已观察 · 等待报价源' : quoteReady ? '报价源已连接 · 等待账户核验' : '等待报价与账户核验'}${venueScope}`;
            const blockerCount = Array.isArray(data.blockers) ? data.blockers.length : 0;
            const detailDisclosure=get('ops-detail-disclosure'), detailSummary=get('ops-detail-summary');
            if(detailSummary) detailSummary.textContent=blockerCount ? `${blockerCount} 项待处理 · 点击查看` : `${selectedVenues.length} 个平台 · 当前无阻塞`;
            if(detailDisclosure && blockerCount) detailDisclosure.open=true;
            get('ops-summary').textContent = data.execution.running
                ? '自动执行运行中 · 请勿重复启动'
                : !executionScopeReady
                    ? selectedVenues.length>2
                        ? '行情扫描可多选 · 等待新鲜报价后自动绑定最优两腿 · 当前已选 '+selectedVenues.length+' 个'
                        : '实盘执行至少需要两个平台 · 当前已选 '+selectedVenues.length+' 个'
                : selectedVenues.length>2
                    ? '已选 '+selectedVenues.length+' 个平台 · 当前最优执行组合：'+venueListText(executionPair)
                : blockerCount
                    ? `待处理 ${blockerCount} 项 · 先做只读检查`
                    : '可做只读检查 · 不会自动下单';
            get('ops-state').textContent = '全局硬上限 '+data.notional_cap+' USDC · 更新 '+date(data.observed_at);
            const latestExecutionEvent=data.execution.latest_event;
            const reconcileState=data.execution.reconciliation_state;
            const reconcileText=reconcileState==='waiting' ? ' · 自动只读复核中 · 未发送订单' :
                reconcileState==='flat_confirmed' ? ' · 自动只读核对已确认空仓' : '';
            const resumeText=reconcileState==='resume_ready' ? ' · 自动只读核对已确认原持仓 · 可恢复监控' : '';
            const activePolicy = data.execution.target_notional ?
                ` · 单腿开仓累计 ${data.execution.filled_notional || 0} / ${data.execution.target_notional} USDC` : '';
            const cyclePolicy = data.execution.position_policy?.mode === 'spread_grid' ? ' · 价差网格' : data.execution.single_position_cycle === false ? ' · 按并发上限' : ' · 单仓循环';
            const fallbackPolicy = data.execution.candidate_fallback === false ? ' · 失败停止' : ' · 自动切换候选';
            const closeOnlyPolicy = data.execution.close_only ? ' · 只平仓模式' : '';
            const entryBackoff = data.entry_scan_backoff_reason ? ' · '+data.entry_scan_backoff_reason : '';
            const processDetail=(data.execution.note||latestExecutionEvent&&executionEventText(latestExecutionEvent)||'').trim();
            const processText='状态：'+executionStateLabel(data.execution.state)+
                (running?' · 新仓范围：'+venueListText(platformScope(data.execution).active)+' · '+(data.execution.close_only?'仅管理已有仓位，不开新仓':'自动开仓与平仓'):'')+
                (processDetail?' · '+compactStatusText(processDetail):'')+
                (latestExecutionEvent?' · 当前：'+compactStatusText(executionEventText(latestExecutionEvent)): '')+
                reconcileText+resumeText;
            const processState=get('execution-process-state');
            processState.textContent=processText;
            const positionMonitorNode=get('execution-position-monitor');
            if(positionMonitorNode&&!globalThis.positionMonitorStreamActive){
                positionMonitorNode.textContent=globalThis.positionMonitorSummary?.(
                    data.execution.position_monitor, data.execution.tracked_position_count||0)||'';
                positionMonitorNode.className='chart-note '+(
                    data.execution.position_monitor?.entry_paused||data.execution.position_monitor?.heartbeat_stale?'warn':'');
            }
            const modeState=get('execution-position-mode');
            if(modeState){
                const policy=data.execution.position_policy;
                modeState.textContent=policy?.mode==='spread_grid'
                    ? `进程记录模式：价差网格 · 当前 ${policy.active_layers||0} 层 · 每格 ${policy.step_bps} bps · 最多 ${policy.max_layers} 层 · 已用 ${Number(policy.used_notional_usdc||0).toFixed(2)} / ${policy.total_notional_usdc} USDC`
                    : policy?.mode==='single'?'进程记录模式：单开':'当前进程尚未上报持仓模式；新规则需停止旧任务后重新只读检查并启动。';
            }
            const routePauses=get('execution-route-pauses');
            const riskNames={edge:'净价差不足',price:'价格越界',timeout:'超时',amount:'金额上限',zero_fill:'零成交',partial_fill:'部分成交',rejected:'拒单',quantity:'数量不匹配',accounting:'费用待核对',unknown:'其他待核对'};
            if(routePauses){
                const pauses=Array.isArray(data.execution.route_pauses)?data.execution.route_pauses:[];
                routePauses.hidden=!pauses.length;
                routePauses.textContent=pauses.length?'连续回补暂停：'+pauses.map(p=>
                    `${p.symbol} · ${venueListText([p.buy_venue,p.sell_venue]).replace(' ↔ ',' → ')} · ${p.rollback_count} 次${riskNames[p.reason_code]?' · '+riskNames[p.reason_code]:''} · 至 ${new Date(p.paused_until*1000).toLocaleString('zh-CN',{hour12:false})}`
                ).join('；')+'。其余路线继续检查，已有仓位仍可平仓。':'';
            }
            const riskStatus=get('execution-entry-risk');
            if(riskStatus){
                const routes=Array.isArray(data.execution.entry_risk_routes)?data.execution.entry_risk_routes:[];
                const counts={};
                routes.forEach(r=>Object.entries(r.counts||{}).forEach(([k,n])=>{if(riskNames[k]&&Number.isFinite(n)&&n>0)counts[k]=(counts[k]||0)+n;}));
                const total=Object.entries(counts).map(([k,n])=>riskNames[k]+' '+n+' 次').join(' · ');
                const configuredBudgetActive=Number(data.execution.position_monitor?.entry_budget_policy_version||0)>=2;
                riskStatus.textContent='历史执行窗口仅作观察，不自动加大价格预算或扣减入场收益。'+
                    (data.execution.running&&!configuredBudgetActive?' 当前执行进程尚未加载此规则，下次正常启动后生效。':'')+
                    (total?' 本组合回补主因累计：'+total+'。':' 新分类尚无记录，旧订单不推测归类。');
                riskStatus.title=routes.map(r=>r.route+' · 新鲜样本 '+r.samples+(r.samples>=5?' · P90 '+Number(r.movement_bps).toFixed(2)+' bps / '+Math.round(r.window_ms)+' ms':' · 样本不足')+(Number.isFinite(r.size_cap_usdc)?' · 下次每腿缩量上限 '+r.size_cap_usdc.toFixed(2)+' USDC':'')).join('\n');
            }
            processState.title='执行状态：'+(data.execution.state||'未知')+' · '+(data.execution.note||'')+
                (data.execution.mode ? cyclePolicy+fallbackPolicy+closeOnlyPolicy : '')+
                (latestExecutionEvent?' · 当前进度：'+executionEventText(latestExecutionEvent):'')+entryBackoff+reconcileText+resumeText;
            get('execution-mode').textContent=!data.execution.running&&data.execution.stop_completed?'全局实盘已停止':!data.execution.running&&data.execution.stop_requires_reconcile&&data.execution.stop_requested?'已停止 · 仓位待核对':data.execution.running?(data.execution.recovery_blocked?'旧仓位待接管':data.execution.close_only&&!data.execution.stop_requested?'只平仓监控中 · 不开新仓':data.execution.managed_positions?.length&&!data.execution.stop_requested?'原仓位已接管 · 监控中':data.execution.mode==='continuous_monitor'?(data.execution.stop_requested?drainStatus(data.execution).label:'全局实盘运行中 · 未确认成交'):['once','wait_once'].includes(data.execution.mode)?'单次验证中 · 等待平仓':'执行中 · 未确认成交'):reconcileState==='waiting'?'自动核对中 · 未下单':reconcileState==='resume_ready'?'已核对原持仓 · 可恢复监控':reconcileState==='flat_confirmed'?'已自动核对空仓 · 可重新只读检查':data.execution.latest_event?.event==='close_receipt'?'单次验证已完成一轮开仓与平仓':data.execution.latest_event?.event==='entry_receipt'&&Number(data.execution.latest_event.detail?.filled_size||0)>0?'已收到成交回执':data.execution.latest_event?'已结束 · 未确认成交':data.execution.state==='idle'?'监控模式 · 未启用交易':'执行已退出 · 仓位需核对';
            if(data.execution.running){
                setDisabled('execution-once',true);setDisabled('execution-monitor',true);
                const liveArm=get('live-arm'); if(liveArm) liveArm.setAttribute('aria-pressed','true');
                if (data.execution.mode === 'continuous_monitor') {
                    setDisabled('live-arm', checking || stopRequested);
                    liveLabel(stopRequested ? drainStatus(data.execution).label : platformScope(data.execution).differs ? '停止旧组合新开仓' : data.execution.close_only ? '结束只平仓监控' : '停止新开仓');
                    if(liveArm) liveArm.title=stopRequested
                        ? drainStatus(data.execution).detail
                        : '停止新开仓；已有仓位继续按平仓规则监控，确认空仓后退出';
                    armStatus(stopRequested
                        ? drainStatus(data.execution).detail
                        : (data.execution.close_only?'只平仓监控运行中，不会开新仓。':'全局实盘运行中。')+'点击此按钮停止新开仓；已有仓位继续按规则监控，确认空仓后退出。');
                } else {
                    setDisabled('live-arm',true);
                    liveLabel('单次验证：运行中');
                    armStatus('单次验证正在执行；两腿成交后继续等待平仓，完成一轮后结束。');
                }
            }
            else {
                // A child can finish or crash between two diagnostics polls.
                // Never leave the global live button visually armed after that;
                // it must reflect a live worker, not the last command sent.
                const liveArm=get('live-arm');
                if(liveArm) liveArm.setAttribute('aria-pressed','false');
                stopRequested=false;
                if (checkedUntil <= Date.now()) armed=false;
                if(stopped&&!checking){
                    if(checkState!=='failed' && checkState!=='expired')
                        invalidate('全局实盘已停止：点击“核对原仓位（只读）”，核对通过后重新只读检查。');
                    else if(liveArm) liveArm.title='停止标记仍在，先完成账户核对并解除停止。';
                } else if(checkedUntil>Date.now()) {
                    setDisabled('live-arm',false);if(liveArm) liveArm.title='只读检查已通过；点击即可启动全局实盘等待。';liveLabel(executionPolicy().close_only?'启动只平仓监控':'全局实盘：可开启');updateArmStatus();
                } else {
                    setDisabled('live-arm',true);liveLabel('全局实盘：关闭');
                    if(liveArm) liveArm.title=data.blockers?.length?'只读检查尚未通过：'+data.blockers.join('；'):'先完成下方只读检查。';
                    if(data.execution.state==='failed') armStatus('执行进程已停止；请查看当前进度和账户状态，核对后再重新检查。');
                    else updateArmStatus();
                }
            }
            const next=get('ops-next-step');
            if(runtimeScope(data.execution)?.version===1 && data.execution.running)next.textContent=
                (runtimeScope(data.execution).message || '后续新仓按当前生效范围比较最优组合；旧仓在原平台管理。')+
                (data.execution.stop_requested?' 当前仍已停止新开仓；需要主动点击“恢复新开仓”。':'');
            else if(platformScope(data.execution).differs && data.execution.stop_requested)next.textContent='当前仍管理 '+venueListText(data.execution.venues)+' 原组合；改选尚未生效。'+drainStatus(data.execution).detail;
            else if(platformScope(data.execution).differs)next.textContent='当前仍执行 '+venueListText(data.execution.venues)+'；改选尚未生效。先停止旧组合新开仓，旧仓在原平台按规则平仓；空仓且进程退出后核对账户、解除停止，再检查并启动新组合。';
            else if(data.execution.running && data.execution.stop_requested)next.textContent=drainStatus(data.execution).detail+
                (data.execution.resume_control?.available?' 如需按已保存上限继续加组，点击“恢复新开仓”。':'');
            else if(data.execution.running)next.textContent=data.execution.close_only?'当前仅管理已有仓位，不会开新仓；切换为自动开仓与平仓需结束本次运行，再选择模式、重新检查并启动。':'当前有执行进程，组合和执行模式在启动时固定；修改上方选择只影响行情和下一次启动。达到阈值后才会进入提交前检查。';
            else if(data.execution.stop_requires_reconcile&&data.execution.stop_requested)next.textContent='旧进程已停止，仓位与停止保护已保留；点击“核对原仓位（只读）”，通过后重新只读检查并手动开启接管。';
            else if(data.execution.stop_completed&&data.execution.stop_requested)next.textContent='全局实盘已停止，停止标记已保留；重新启动前点击“核对账户并解除停止”，再检查新组合。';
            else if(!executionPairReady)next.textContent=selectedVenues.length>2
                ? '行情扫描可以同时选择多个平台；等待新鲜候选后，程序会在提交前绑定净收益最高的两腿。'
                : '行情扫描可以同时选择多个平台；实盘至少需要选择两个平台。';
            else if(selectedVenues.length>2)next.textContent='已选多个平台；只读检查和启动时会绑定当前整轮净收益最高的两腿，绑定后本轮执行期间不自动换平台。';
            else if(checkedUntil>Date.now())next.textContent='只读检查已通过；现在可以选择“实盘单次验证”，或在执行区开启全局实盘等待下一次机会。';
            else if(data.execution.state==='failed')next.textContent='执行进程已退出；先核对账户与持仓，再重新做只读检查。';
            else if(data.execution.latest_event){
                const historical = historicalExecutionEvent(data.execution, data.execution.latest_event);
                next.textContent=(historical?'上次历史结果（'+date(data.execution.latest_event.timestamp)+'）：':'最近结果：')+
                    executionEventText(data.execution.latest_event)+'。如需再次执行，先核对账户和执行历史。';
            }
            else if(stopped)next.textContent='全局实盘已停止。点击“核对账户并解除停止”；通过后重新只读检查，不会自动下单。';
            else if(data.blockers?.length)next.textContent='现在不能实盘：按下列检查项逐项处理。已保存的密钥不用重填；已有手动开平仓样本不用重复交易。浏览器只读桥接不等于自动下单连接。';
            else next.textContent='接入配置已通过：先检查当前市场最低金额和每腿上限；只读检查通过后，可单次验证一单，或在执行区开启全局实盘。';
            const root = get('ops-connections');root.replaceChildren();
            const selectedLabels = selectedVenues.length ? selectedVenues.map(value => ({variational:'Variational',lighter:'Lighter 主网',lighter_rh:'Lighter RH',standx:'StandX',binance:'Binance Futures',gate:'Gate Futures',okx:'OKX Swap',arcus:'Arcus'}[value] || value)).join('、') : '尚未选择';
            line(root,'行情扫描平台：'+selectedLabels);
            const executionPairText = executionPair && executionPair.length===2 && executionPair.every(venue=>['lighter','lighter_rh','standx','arcus'].includes(venue))
                ? executionPair.map(value=>({lighter:'Lighter 主网',lighter_rh:'Lighter RH',standx:'StandX',arcus:'Arcus'}[value]||value)).join(' ↔ ')
                : selectedVenues.length>2
                    ? '行情扫描多选 · 等待新鲜候选后自动绑定最优两腿'
                : selectedVenues.includes('variational')&&selectedVenues.includes('lighter')
                    ? 'Variational ↔ Lighter 主网'
                    : '尚未选择已支持的实盘组合';
            line(root,running ? '正在执行的新仓范围：'+venueListText(platformScope(data.execution).active) : '下次启动的实盘组合：'+executionPairText);
            if(platformScope(data.execution).differs) line(root,'当前改选尚未切换实盘：'+venueListText(selectedVenues));
            const platformStatus = data.platforms || {};
            const platformNames = {lighter:'Lighter 主网', lighter_rh:'Lighter RH', standx:'StandX', arcus:'Arcus'};
            const executionVenues = selectedVenues.filter(value => platformNames[value]);
            (executionVenues.length ? executionVenues : ['lighter']).forEach(venue => {
                const status = platformStatus[venue] || {};
                const fee = status.taker_fee_bps == null ? '费率待核验' : 'Taker '+(Number(status.taker_fee_bps)/100)+'%';
                line(root, platformNames[venue]+'：'+(status.authenticated?'只读鉴权有效 · '+fee+' · 活动订单 '+(status.active_orders ?? '未知'):
                    status.saved?'密钥已保存 · 需要只读复查':'未配置密钥'));
            });
            line(root,'实时行情：Lighter 主网 '+(data.feeds.lighter.connected?'原生 WebSocket 已连接':'WebSocket 未连接')+'；Lighter RH 价差 API 使用原生 WebSocket，断线自动 REST 降级');
            if(selectedVenues.includes('variational')){
                const browser=data.browser||{};
                line(root,'Variational 浏览器：'+(browser.reachable?'已连接 '+(browser.address||'本机 CDP'):'未连接')+' · '+(browser.mode||'需要本机 CDP')+
                    (browser.reachable?' · '+(browser.matching_pages===1?'目标市场页就绪':'目标市场页数量 '+browser.matching_pages):' · '+(browser.error||'请先启动')));
                line(root,'网页报价：'+(data.bridge.symbols?.length?data.bridge.symbols.join(' / '):'等待配对后报价')+'；手动采集最后时间 '+date(c.latest_at));
                line(root,'Variational 当前持仓：'+(c.position.known?(c.position.rows.length?'已收到持仓，需按账户核对':'收到完整空持仓快照'):'未知'));
                line(root,'手续费/资金费：'+(c.settlement_values_observed?'已捕获结算字段，仍需按成交 RFQ 核对':'尚未捕获可核对的实际结算字段；净收益未知'));
            }
            table(get('ops-settlements'),['观察时间','类型','资产','数量','手续费','手续费币种','资金费','资金费币种','成交关联','状态'],(c.settlements||[]).map(x=>[date(x.observed_at),x.transfer_type||x.transaction_type||x.type,x.asset||x.token,x.amount??x.qty,x.fee??x.fee_amount,x.fee_asset||x.fee_currency,x.funding_fee??x.funding_payment??x.funding_amount??x.settled_funding,x.funding_asset||x.funding_currency,x.linked_to_confirmed_trade?'关联已确认成交':'未关联',x.status||x.clearing_status]));
            const blockers=get('ops-blockers');blockers.replaceChildren();
            if(checkState==='expired') line(blockers,'只读检查已过期；点击“只读检查”重新核对账户、金额和市场。');
            else if(checkState==='checking') line(blockers,'只读检查进行中；此过程不会提交订单。');
            else if(checkState==='passed' && checkedUntil>Date.now()) line(blockers,'只读检查已通过；现在可以选择“实盘单次验证”，或在执行区开启全局实盘。');
            else if(data.blockers?.length)data.blockers.forEach((text,i)=>line(blockers,(i+1)+'. '+text));
            else line(blockers,'静态配置已通过；点击“只读检查”核对当前金额、账户与市场。');
            const evidence=get('ops-evidence-checks');evidence.replaceChildren();
            (data.readiness||[]).forEach(item=>{const box=document.createElement('div');box.className='ops-blocker-item';const title=document.createElement('b');title.textContent=(item.status==='passed'?'✓ ':item.status==='observed'?'已观察 · ':'待核对 · ')+item.title;box.appendChild(title);line(box,item.evidence);line(box,item.action);evidence.appendChild(box);});
            get('ops-capture-summary').textContent = c.window_events+' 条事件'+(c.window_truncated?'（最近 1 MB）':'')+' · 完整关联 '+c.chains.filter(x=>x.linked).length+' 组';
            get('ops-capture-note').textContent = c.note;
            const warnings=get('ops-capture-warnings');warnings.replaceChildren();c.warnings.forEach(x=>line(warnings,x));
            table(get('ops-evidence'),['观察时间','市场','方向','数量','成交价','平台状态','编号'],c.evidence.map(x=>[date(x.observed_at),x.symbol,x.side==='buy'?'买':'卖',x.qty,x.price,x.status,x.identity_available?'已保留指纹':'旧录制缺失']));
            const chains=get('ops-chains');chains.replaceChildren();
            c.chains.forEach(x=>line(chains,date(x.observed_at)+' · '+x.side+' · RFQ '+(x.rfq_received?'已收到':'待返回')+' · 关联订单 '+x.orders_linked+' · 关联成交 '+x.trades_linked+' · '+(x.linked?'数量/价格/账户一致':'关联待核对')+'（手动观测，非自动执行验收）'));
            table(get('ops-log'),['时间','事件','接口 / HTTP','诊断编号','位置'],data.diagnostics.map(x=>[date(x.at),x.message,(x.route||'后台')+(x.status?' / '+x.status:''),x.event_id,x.location||'—']));
        } catch (error) {
            get('readiness-summary').textContent='诊断连接中断 · 当前账户与实盘条件未知';
            get('ops-summary').textContent='诊断连接中断 · 显示内容可能已过期';
            get('ops-state').textContent='读取失败：'+error.message;
        } finally {clearTimeout(timer);pending=false;}
    }
    document.addEventListener('DOMContentLoaded',()=>{
        const status=document.createElement('p');status.id='execution-arm-status';status.className='chart-note';status.setAttribute('role','status');
        const checkResult=get('execution-check-result');
        const primaryActions=get('execution-primary-actions');
        if(primaryActions)primaryActions.before(status);
        else if(checkResult)checkResult.before(status);
        const liveArm=get('live-arm');
        if(liveArm)liveArm.setAttribute('aria-describedby','execution-arm-status');
        syncExecutionScope();
        invalidate();setInterval(updateArmStatus,1000);
        bind('ops-refresh','onclick',refresh);
        bind('browser-open','onclick',openVariational);
        bind('quote-observer-start','onclick',()=>observerAction('start'));
        bind('quote-observer-stop','onclick',()=>observerAction('stop'));
        bind('execution-check','onclick',()=>executionAction('check'));
        bind('execution-prepare','onclick',()=>executionAction('prepare'));
        bind('execution-preview','onclick',()=>executionAction('preview'));
        bind('live-arm','onclick',()=>{
            if(running){
                executionAction('stop');
                return;
            }
            if(Date.now()>checkedUntil){invalidate();const result=get('execution-check-result');if(result)result.textContent='只读检查已过期；请重新检查，未启用实盘。';return;}
            armed=true;
            const liveArm=get('live-arm');
            if(liveArm) liveArm.setAttribute('aria-pressed','true');
            updateArmStatus();
            const result=get('execution-check-result');
            if(result)result.textContent='执行区全局实盘已开启（仅本页）；正在启动持续监控，不会立即发单。';
            executionAction('monitor');
        });
        bind('execution-once','onclick',()=>executionAction(executionSelection().mode==='all'?'wait-once':'once'));
        bind('execution-monitor','onclick',()=>executionAction('monitor'));
        bind('execution-stop','onclick',()=>executionAction('stop'));
        bind('execution-resume','onclick',resumeEntries);
        bind('execution-apply-scope','onclick',applyEntryScope);
        bind('execution-reset-stop','onclick',()=>executionAction('reset-stop'));
        bind('execution-temporary-cap','onchange',()=>{syncExecutionCapLimit();syncExecutionPolicy();renderMinimumHint();invalidate('金额模式已修改，请重新执行只读检查。');});
        bind('execution-cap','oninput',()=>{syncExecutionPolicy();renderMinimumHint();invalidate('金额已修改，请重新执行只读检查。');});
        bind('execution-target','oninput',()=>{syncExecutionPolicy();invalidate('目标金额已修改，请重新执行只读检查。');});
        bind('execution-fallback','onchange',()=>{syncExecutionPolicy();invalidate('候选切换策略已修改，请重新执行只读检查。');});
        bind('execution-trade-mode','onchange',()=>{
            if(running){syncExecutionMode();return;}
            requestedCloseOnly=get('execution-trade-mode')?.value==='close_only';
            syncExecutionMode();syncExecutionPolicy();invalidate('本次执行模式已修改，请重新执行只读检查。');
        });
        const markStrategyDirty = reason => { strategyDirty=true; syncStrategySummary(); syncExecutionPolicy(); renderEntryLimits(); renderResumeControl(); invalidate(reason); };
        strategyControlIds.forEach(id=>{
            // Channel edits are staged in their modal. Saving dispatches the
            // entry-mode change; cancelling must not dirty the main settings.
            if(id.startsWith('channel-'))return;
            bind(id,'onchange',()=>markStrategyDirty('价差策略已修改，请保存策略并重新执行只读检查。'));
            bind(id,'oninput',()=>markStrategyDirty('价差策略已修改，请保存策略并重新执行只读检查。'));
        });
        if (typeof globalThis.addEventListener === 'function') {
            globalThis.addEventListener('execution-platform-scope-ready',()=>{
                clearTimeout(scopeTimer);
                scopeTimer = setTimeout(applyEntryScope, 350);
            });
            globalThis.addEventListener('execution-selection-changed',()=>{
                invalidate(runtimeScope(latestOperations?.execution)?.version===1
                    ? '选择已修改；新仓平台待后台核验后应用，已有仓位继续在原平台管理。'
                    : '参与平台、品种或筛选条件已修改，请重新执行只读检查；运行中的实盘组合保持启动时选择。');
                syncExecutionScope();renderPlatformScope();
                if(latestOperations) renderExecutionBanner(latestOperations.execution);
                refresh();
            });
            globalThis.addEventListener('strategy-config-loaded', syncExecutionCapLimit);
            globalThis.addEventListener('strategy-config-values-loaded', event=>{
                const trading=event?.detail?.trading;
                if(trading) savedEntryLimits={position_size:trading.position_size??100,
                    max_positions:trading.max_positions??1,max_total_position_notional:trading.max_total_position_notional??0,
                    capital_reserve_percent:trading.capital_reserve_percent??0,capital_reserve_usdc:trading.capital_reserve_usdc??0,
                    capital_allocation_mode:trading.capital_allocation_mode??'per_leg'};
                syncExecutionCapLimit();syncStrategySummary();renderEntryLimits();
            });
            globalThis.addEventListener('strategy-config-saved',()=>{strategyDirty=false;syncStrategySummary();syncExecutionCapLimit();renderEntryLimits();renderResumeControl();});
        }
        bind('execution-open-settings','onclick',()=>get('settings-open')?.click());
        bind('save-execution-strategy','onclick',async()=>{
            const result=get('execution-strategy-result');
            if(typeof globalThis.saveStrategyConfig!=='function'){if(result)result.textContent='配置模块尚未加载，请刷新页面后重试。';return;}
            if(result)result.textContent='正在保存策略…';
            await globalThis.saveStrategyConfig();
            if(!/保存失败/.test(get('config-result')?.textContent||'')) strategyDirty=false;
            renderResumeControl();
            if(result)result.textContent=get('config-result')?.textContent||'策略已保存；数量与金额用于新开仓，其他设置待全部平仓。';
        });
        syncExecutionMode();syncExecutionPolicy();
        syncExecutionCapLimit();
        refreshMinimumHint(true);
        syncStrategySummary();
        refresh();setInterval(()=>{if(!document.hidden)refresh();},5000);
    });
})();
