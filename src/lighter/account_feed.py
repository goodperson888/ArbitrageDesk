"""Account notifications invalidate REST evidence, never extend its lifetime.

The official SDK WsClient uses account_all/{account_index}. Partial updates
are never interpreted as an authoritative empty account.
"""
import json
from src.common.live_feed import SocketFeed


class LighterAccountFeed(SocketFeed):
    def __init__(self, account_index, ws_url, proxy_url=None):
        super().__init__(ws_url, proxy_url)
        self.account_index = int(account_index)
        self.revision = 0

    def reset(self):
        self.revision += 1

    def subscribe(self, ws):
        ws.send(json.dumps({'type': 'subscribe', 'channel': f'account_all/{self.account_index}'}))

    def on_message(self, message):
        if message.get('type') == 'error':
            raise ValueError('Lighter 账户推送订阅失败')
        if (message.get('type') in ('subscribed/account_all', 'update/account_all') and
                message.get('channel') in (f'account_all:{self.account_index}',
                                           f'account_all/{self.account_index}')):
            self.revision += 1

    def token(self):
        with self.lock:
            return self.connected, self.revision
