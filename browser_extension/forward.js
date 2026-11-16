window.addEventListener('message', event => {
    if (event.source !== window || event.origin !== 'https://omni.variational.io') return;
    if (event.data?.type === 'VARIATIONAL_READ_ONLY_QUOTE_V1') {
        chrome.runtime.sendMessage({type: 'quote', quote: event.data.quote}).catch(() => {});
    } else if (event.data?.type === 'VARIATIONAL_MANUAL_EVENT_V1') {
        chrome.runtime.sendMessage({type: 'manual_event', event: event.data.event}).catch(() => {});
    }
});
