const status = document.getElementById('status');
chrome.storage.local.get(['token', 'lastStatus']).then(data => { status.textContent = data.lastStatus || (data.token ? '已配对，等待报价' : '未配对'); });
document.getElementById('save').onclick = async () => {
    const token = document.getElementById('token').value.trim();
    if (token.length < 30 || token.length > 100) { status.textContent = '请粘贴完整配对码'; return; }
    await chrome.storage.local.set({token, lastStatus: '已配对，等待报价'});
    document.getElementById('token').value = ''; status.textContent = '已配对，请刷新 Variational 页面';
};
document.getElementById('disconnect').onclick = async () => { await chrome.storage.local.clear(); status.textContent = '已断开'; };
