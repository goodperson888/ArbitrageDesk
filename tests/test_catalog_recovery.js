'use strict';
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
let now = 100000, release, mode = 'failure';
const requests = [], streams = [], timers = new Map(), nodes = new Map();
let timerId = 0;
function element() {
    return {children: [], textContent: '', classList: {toggle() {}},
        replaceChildren() {this.children = [];}, appendChild(child) {this.children.push(child);},
        append(...children) {this.children.push(...children);}, setAttribute() {}, addEventListener() {}};
}
for (const id of ['v2-opportunity-result', 'v2-catalog-summary', 'v2-api-state']) nodes.set(id, element());
const instrument = (symbol, extra = {}) => ({canonical_symbol: symbol + '-PERP', ...extra});
const context = vm.createContext({
    window: {QuoteFreshness: {}}, document: {getElementById: id => nodes.get(id),
        createElement: element, querySelectorAll: () => [], addEventListener() {}},
    localStorage: {getItem: () => null}, AbortController, console,
    Date: class extends Date {static now() {return now;}}, performance: {now: () => now},
    location: {protocol: 'http:'}, WebSocket: class {
        constructor(url) {streams.push(url); this.readyState = 0;} close() {this.readyState = 3;}
    },
    setTimeout(fn, ms) {const id = ++timerId; timers.set(id, {fn, ms}); return id;},
    clearTimeout(id) {timers.delete(id);},
    fetch: async url => {
        requests.push(url);
        if (url.includes('/opportunities?')) return {ok: true, headers: {get: () => null}, json: async () => []};
        const venue = new URL(url).searchParams.get('venue');
        if (venue !== 'lighter_rh') return {ok: true, json: async () => [instrument('BTC'), instrument('ETH')]};
        if (mode === 'held') await new Promise(resolve => {release = resolve;});
        if (mode === 'success') return {ok: true, json: async () => [instrument('SOL')]};
        if (mode === 'empty') return {ok: true, json: async () => []};
        return {ok: false, status: 503, json: async () => ({detail: 'RH 品种目录暂不可用：本地代理不可用或直连失败'})};
    }
});
const source = fs.readFileSync('src/web/static/js/cross_venue.js', 'utf8');
vm.runInContext(source.replace(/\}\)\(\);\s*$/,
    'window.test = {state, refreshCatalog, commonSymbols, scan, venueHealth};})();'), context);
const {state, refreshCatalog, commonSymbols, scan} = context.window.test;
state.selectedVenues = ['lighter', 'lighter_rh', 'arcus'];
state.venues = state.selectedVenues.map(venue => ({venue}));
state.auto = false;
const rhCalls = () => requests.filter(url => url.includes('/instruments?venue=lighter_rh')).length;
const tick = () => new Promise(resolve => setImmediate(resolve));
(async () => {
    mode = 'held';
    const first = refreshCatalog(), second = refreshCatalog();
    await tick();
    assert.equal(requests.length, 3, 'overlapping refreshes share one request per venue');
    release(); await Promise.all([first, second]);
    assert.match(nodes.get('v2-catalog-summary').textContent, /RH.*本地代理不可用/);
    assert.match(context.window.test.venueHealth('lighter_rh').detail, /目录自动重试中/);
    assert.equal(commonSymbols(state.selectedVenues).join(','), 'BTC,ETH', 'one missing venue cannot hide a healthy pair');
    assert.equal(timers.get(state.catalogTimer).ms, 10000);
    await refreshCatalog();
    assert.equal(rhCalls(), 1, 'automatic scans obey retry backoff');

    mode = 'failure';
    for (const delay of [20000, 40000, 60000, 60000]) {
        now = state.catalogStatus.lighter_rh.retryAt;
        const timer = timers.get(state.catalogTimer); timers.delete(state.catalogTimer);
        await timer.fn();
        assert.equal(timers.get(state.catalogTimer).ms, delay);
    }
    assert.equal(rhCalls(), 5, 'recovery continues after the former three-attempt cutoff');
    const before = rhCalls();
    await scan(false);
    assert.equal(rhCalls(), before, 'healthy scanning does not hammer the failing catalog');
    const opportunities = requests.filter(url => url.includes('/opportunities?'));
    assert(opportunities.length > 0);
    assert.equal(new URL(opportunities[0]).searchParams.get('venues'), 'lighter,arcus');
    assert.equal(new URL(streams[0]).searchParams.get('venues'), 'lighter,arcus', 'the stream also excludes the unavailable venue');
    assert.deepEqual(Array.from(state.selectedVenues), ['lighter', 'lighter_rh', 'arcus'], 'user selection stays intact');

    mode = 'success';
    now = state.catalogStatus.lighter_rh.retryAt;
    await timers.get(state.catalogTimer).fn();
    assert.equal(state.catalogStatus.lighter_rh.error, undefined);
    assert.equal(state.catalogTimer, null);
    assert.doesNotMatch(nodes.get('v2-catalog-summary').textContent, /目录未完整/);
    assert.equal(requests.filter(url => url.includes('/instruments?') &&
        ['lighter', 'arcus'].includes(new URL(url).searchParams.get('venue'))).length, 2,
        'successful catalogs are retained throughout retries');

    state.instruments.lighter = [instrument('BTC'), instrument('BTC'), instrument('DISABLED', {enabled: false})];
    state.instruments.arcus = [instrument('ETH'), instrument('DISABLED')];
    state.instruments.lighter_rh = [instrument('BTC'), instrument('ETH')];
    assert.equal(commonSymbols(state.selectedVenues).join(','), 'BTC,ETH', 'scan the union of pair intersections');
    state.excludedSymbols = ['BTC-PERP'];
    assert.equal(commonSymbols(state.selectedVenues).join(','), 'ETH');
    assert.equal(commonSymbols(['lighter', 'lighter']).length, 0, 'duplicate venues never form a pair');
    state.selectedVenues = ['lighter', 'arcus'];
    await scan(false);
    assert.match(nodes.get('v2-opportunity-result').children[0].textContent, /目录已加载.*没有可扫描/);
    assert.doesNotMatch(nodes.get('v2-opportunity-result').children[0].textContent, /尚未返回/);

    state.selectedVenues = ['lighter_rh']; delete state.instruments.lighter_rh; mode = 'empty';
    await refreshCatalog(true);
    assert.match(state.catalogStatus.lighter_rh.error, /未返回可用永续品种/);
    assert(state.catalogTimer, 'an empty response is explained and remains recoverable');
    console.log('Catalog recovery, backoff, request sharing, pair coverage and degraded scanning: PASS');
})().catch(error => {console.error(error); process.exitCode = 1;});
