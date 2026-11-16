"""Listen only to observed quote responses in explicitly configured local Chrome.

No navigation, DOM writes, request replay, wallet reads or order submission.
"""
import base64
import json
import threading
import time
from urllib.parse import urlsplit
import requests
import websocket
from src.common.market_data import utc_now
from src.execution.browser_session import valid_debugger_address

QUOTE_URL='https://omni.variational.io/api/quotes/indicative'
QUOTE_PATH='/api/quotes/indicative'


def is_quote_url(url):
    parsed=urlsplit(url or '')
    return (parsed.scheme=='https' and parsed.netloc=='omni.variational.io' and
            parsed.path==QUOTE_PATH)


def quote_body(body):
    if not isinstance(body,dict):
        raise ValueError('quote_response_not_object')
    for envelope in ('result','quote'):
        nested=body.get(envelope)
        if isinstance(nested,dict):
            return nested
    return body


def safe_quote(request, body):
    instrument=request.get('instrument',{})
    if instrument.get('underlying') not in ('ETH','BTC') or instrument.get('instrument_type')!='perpetual_future':
        raise ValueError('unsupported_quote')
    quote=quote_body(body)
    if quote.get('bid') is None or quote.get('ask') is None:
        raise ValueError('quote_fields_missing')
    result={'source_url':QUOTE_URL,'symbol':instrument['underlying'],
        'instrument_type':'perpetual_future','qty':request.get('qty'),
        'bid':quote.get('bid'),'ask':quote.get('ask'),'timestamp':quote.get('timestamp'),
        'observed_at':utc_now(),'quote_id_present':bool(quote.get('quote_id'))}
    margin=quote.get('margin_requirements',{})
    if all(k in margin for k in ('estimated_fees_bid','estimated_fees_ask')):
        result.update({k:margin[k] for k in ('estimated_fees_bid','estimated_fees_ask')})
    if 'qty_limits' in quote:
        result['qty_limits']={side:{k:quote['qty_limits'].get(side,{}).get(k)
            for k in ('min_qty_tick','min_qty','max_qty')} for side in ('bid','ask')}
    return result


def market_target(target):
    parsed=urlsplit(target.get('url',''))
    return (target.get('type')=='page' and parsed.scheme=='https' and
            parsed.netloc=='omni.variational.io' and parsed.path in ('/perpetual/ETH','/perpetual/BTC'))


class PassiveQuoteBridge:
    def __init__(self, bridge):
        self.bridge=bridge
        self.lock=threading.RLock()
        self.thread=None
        self.stop_event=threading.Event()
        self.workers={}
        self.pages={}
        self.received=0
        self.rejected=0
        self.last_rejection=None
        self.error=None
        self.last_scan_at=None
        self.page_health={}

    def _page_status(self, key):
        with self.lock:
            item=self.page_health.get(key)
            return dict(item) if item else None

    def _update_page(self, key, **changes):
        with self.lock:
            item=self.page_health.setdefault(key, {'connected':False,'quote_requests':0,
                'quote_completions':0,'websocket_messages':0,'reconnects':0})
            item.update(changes)
            return dict(item)

    def status(self):
        with self.lock:
            return {'running':bool(self.thread and self.thread.is_alive()),
                    'markets':sorted(set(self.pages.values())), 'received':self.received,
                    'rejected':self.rejected,'last_rejection':dict(self.last_rejection) if self.last_rejection else None,
                    'error':self.error,'last_scan_at':self.last_scan_at,
                    'pages':{key:dict(value) for key,value in self.page_health.items()},
                    'note':'仅监听程序浏览器已有 BTC/ETH 页面；不会自动切换官网市场或请求报价'}

    def _record_rejection(self, reason):
        with self.lock:
            self.rejected+=1
            self.last_rejection={'reason':reason,'rejected_at':utc_now()}
            self.error='最近报价未接收：'+reason

    def start(self,address):
        if not valid_debugger_address(address):raise ValueError('仅支持已配置的本机调试浏览器')
        with self.lock:
            if self.thread and self.thread.is_alive():return self.status()
            self.stop_event.clear()
            self.thread=threading.Thread(target=self._watch,args=(address,),daemon=True)
            self.thread.start()
        return self.status()

    def _watch(self,address):
        session=requests.Session();session.trust_env=False
        try:
            while not self.stop_event.is_set():
                try:
                    response=session.get('http://'+address+'/json/list',timeout=2);response.raise_for_status()
                    targets=[t for t in response.json() if market_target(t)]
                    with self.lock:
                        self.last_scan_at=utc_now()
                    # Duplicate pages for one market are ambiguous: skip that market.
                    counts={p:sum(t['url'].split('?')[0]==p for t in targets) for p in {t['url'].split('?')[0] for t in targets}}
                    with self.lock:
                        if not targets:
                            self.error='程序浏览器没有打开 BTC/ETH 页面'
                        elif self.error=='程序浏览器没有打开 BTC/ETH 页面':
                            self.error=None
                    for target in targets:
                        if counts[target['url'].split('?')[0]]!=1:continue
                        key=target['id'];ws=urlsplit(target.get('webSocketDebuggerUrl',''))
                        if ws.scheme!='ws' or ws.netloc!=address:continue
                        old=self.workers.get(key)
                        if old is None or not old.is_alive():
                            previous=self._page_status(key)
                            self._update_page(key, url=target['url'], market=urlsplit(target['url']).path.rsplit('/',1)[-1],
                                connected=False, error=None,
                                reconnects=(previous or {}).get('reconnects',0)+(1 if previous else 0))
                            worker=threading.Thread(target=self._listen,args=(key,target),daemon=True)
                            self.workers[key]=worker;worker.start()
                    self.workers={k:w for k,w in self.workers.items() if w.is_alive()}
                except Exception:
                    with self.lock:self.error='程序浏览器不可达；检查已登录的本机调试会话'
                self.stop_event.wait(3)
        finally:session.close()

    def _listen(self,key,target):
        sock=None
        try:
            sock=websocket.create_connection(target['webSocketDebuggerUrl'],timeout=1,suppress_origin=True,http_no_proxy=['127.0.0.1','localhost'])
            command=1;sock.send(json.dumps({'id':command,'method':'Network.enable','params':{}}))
            pending={};bodies={}
            self._update_page(key, connected=True, connected_at=utc_now(), last_event_at=utc_now(), error=None)
            with self.lock:self.pages[key]=urlsplit(target['url']).path.rsplit('/',1)[-1]
            while not self.stop_event.is_set():
                try:raw=sock.recv()
                except websocket.WebSocketTimeoutException:continue
                if not raw:break
                event=json.loads(raw);params=event.get('params',{});request_id=params.get('requestId');method=event.get('method')
                self._update_page(key, last_event_at=utc_now())
                if method=='Network.webSocketFrameReceived':
                    self._update_page(key, websocket_messages=(self._page_status(key) or {}).get('websocket_messages',0)+1,
                        last_websocket_at=utc_now())
                if method=='Network.requestWillBeSent':
                    request=params.get('request',{})
                    if not is_quote_url(request.get('url')) or request.get('method')!='POST':continue
                    page=self._page_status(key) or {}
                    self._update_page(key, quote_requests=page.get('quote_requests',0)+1,
                        last_quote_request_at=utc_now())
                    if len(pending)>=128:pending.clear()
                    try:
                        body=json.loads(request.get('postData',''))
                        instrument=body['instrument']
                        # Retain only the three public quote request fields.
                        pending[request_id]={'qty':body['qty'],'instrument':{'underlying':instrument['underlying'],'instrument_type':instrument['instrument_type']}}
                    except (ValueError,KeyError,TypeError):
                        self._record_rejection('数量报价请求结构无法识别')
                        continue
                elif method=='Network.responseReceived' and request_id in pending:
                    response=params.get('response',{})
                    if not is_quote_url(response.get('url')):
                        pending.pop(request_id,None)
                    elif response.get('status')!=200:
                        pending.pop(request_id,None)
                        self._record_rejection('数量报价接口返回 HTTP '+str(response.get('status')))
                elif method=='Network.loadingFailed':
                    if request_id in pending:
                        pending.pop(request_id,None)
                        self._record_rejection('数量报价请求加载失败')
                elif method=='Network.loadingFinished' and request_id in pending:
                    command+=1
                    page=self._page_status(key) or {}
                    self._update_page(key, quote_completions=page.get('quote_completions',0)+1,
                        last_quote_completion_at=utc_now())
                    if len(bodies)>=128:bodies.clear()
                    bodies[command]=pending.pop(request_id)
                    sock.send(json.dumps({'id':command,'method':'Network.getResponseBody','params':{'requestId':request_id}}))
                elif event.get('id') in bodies:
                    request=bodies.pop(event['id']);result=event.get('result',{})
                    raw=result.get('body','')
                    if len(raw)>100000:continue
                    try:
                        if result.get('base64Encoded'):raw=base64.b64decode(raw).decode()
                        payload=safe_quote(request,json.loads(raw))
                        self.bridge.ingest(payload)
                        with self.lock:
                            self.received+=1;self.pages[key]=payload['symbol'];self.error=None
                    except (ValueError,TypeError,KeyError) as exc:
                        reason=str(exc) or '数量报价未通过字段校验'
                        self._record_rejection(reason)
                        continue
        except Exception as exc:
            self._update_page(key, connected=False, error='网页响应监听已断开，正在重连')
            with self.lock:self.error='网页响应监听已断开，正在重连'
        finally:
            self._update_page(key, connected=False)
            with self.lock:self.pages.pop(key,None)
            if sock:sock.close()
