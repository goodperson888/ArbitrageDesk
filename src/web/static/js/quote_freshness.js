/* Quote age advances even when polling is paused or the connection fails. */
(function (root) {
    'use strict';
    const valid = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
    function stamp(row, received = Date.now()) {
        row._receivedAt = received;
        const observed = Date.parse(row.observed_at || '');
        row._deliverySeconds = Number.isFinite(observed) ? Math.max(0, (received - observed) / 1000) : 0;
        return row;
    }
    function describe(row, now = Date.now()) {
        const elapsed = valid(row._receivedAt) ? Math.max(0, (now - row._receivedAt) / 1000) : 0;
        const extra = elapsed + (row._deliverySeconds || 0);
        const ages = ['buy', 'sell'].map(side => valid(row[side + '_age_seconds']) ? row[side + '_age_seconds'] + extra : null);
        const text = seconds => seconds == null ? '未知' : seconds.toFixed(1) + ' 秒前';
        const stale = row.buy_stale || row.sell_stale || ages.some(value => value == null || value > 10);
        const source = value => /websocket/i.test(value || '') ? 'WS 盘口' : value === 'lighter_orderbook' || /rest|public_api/i.test(value || '') ? 'REST 快照' : (value || '来源未知');
        const buyLabel = source(row.buy_source) + ' · ' + text(ages[0]);
        const sellLabel = source(row.sell_source) + ' · ' + text(ages[1]);
        return {stale, ages, buyLabel, sellLabel,
            label: '买 ' + text(ages[0]) + ' · 卖 ' + text(ages[1]),
            detail: '买入：' + buyLabel + '；卖出：' + sellLabel + '。超过 10 秒为过期；这是快照年龄，不是网络延迟。'};
    }
    root.QuoteFreshness = {stamp, describe};
})(typeof window === 'undefined' ? globalThis : window);
