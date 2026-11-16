"""One-click managed Chrome, reusing only the configured local debugging session."""
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote
from urllib.request import build_opener,ProxyHandler,Request
from src.execution.browser_session import valid_debugger_address

LOCK=threading.Lock()


def chrome_binary():
    if sys.platform=='darwin':
        choices=[Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
                 Path.home()/'Applications/Google Chrome.app/Contents/MacOS/Google Chrome']
    elif sys.platform=='win32':
        choices=[Path(os.environ.get(key,''))/'Google/Chrome/Application/chrome.exe'
                 for key in ('PROGRAMFILES','PROGRAMFILES(X86)','LOCALAPPDATA') if os.environ.get(key)]
    else:
        choices=[Path(p) for name in ('google-chrome','google-chrome-stable','chromium','chromium-browser') if (p:=shutil.which(name))]
    for path in choices:
        if path.is_file():return str(path)
    raise ValueError('未找到 Chrome，请先安装 Google Chrome 后重试。')


def targets(address):
    opener=build_opener(ProxyHandler({}))
    with opener.open('http://'+address+'/json/version',timeout=1) as response:
        version=json.load(response)
    if not isinstance(version,dict) or not str(version.get('Browser','')).startswith(('Chrome/','HeadlessChrome/')):
        raise ValueError('配置端口不是 Chrome 会话；未改动其他程序。')
    with opener.open('http://'+address+'/json/list',timeout=1) as response:
        rows=json.load(response)
    if not isinstance(rows,list):raise ValueError('浏览器标签列表无效')
    return rows


def open_browser(root,address,symbol,proxy=None):
    if not valid_debugger_address(address) or symbol not in ('ETH','BTC'):
        raise ValueError('浏览器配置或市场无效')
    root=Path(root)
    with LOCK:
        reused=True
        try:rows=targets(address)
        except (OSError,ValueError):
            # Do not kill a process or choose another port behind the user's back.
            with socket.socket() as probe:
                probe.settimeout(.5)
                if probe.connect_ex(('127.0.0.1',int(address.split(':')[1])))==0:
                    raise ValueError('浏览器连接端口已被占用但无法识别；请在设置中检查连接。') from None
            binary=chrome_binary()
            profile=root/'data/variational-browser'
            if profile.is_symlink():raise ValueError('专用浏览器目录不能是符号链接')
            profile.mkdir(mode=0o700,parents=True,exist_ok=True);profile.chmod(0o700)
            args=[binary,'--remote-debugging-address=127.0.0.1','--remote-debugging-port='+address.split(':')[1],
                  '--user-data-dir='+str(profile.resolve()),'--no-first-run','--no-default-browser-check']
            if proxy:
                from urllib.parse import urlsplit
                parsed=urlsplit(proxy)
                if parsed.scheme not in ('http','https','socks5') or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError('浏览器代理配置无效')
                args.append('--proxy-server='+proxy)
            args.append('https://omni.variational.io/perpetual/'+symbol)
            subprocess.Popen(args,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
            reused=False
            deadline=time.monotonic()+8
            while True:
                try:rows=targets(address);break
                except (OSError,ValueError):
                    if time.monotonic()>=deadline:raise ValueError('浏览器启动中或未就绪，请稍后再点连接；不会重复下单。') from None
                    time.sleep(.2)
        url='https://omni.variational.io/perpetual/'+symbol
        matches=[t for t in rows if t.get('type')=='page' and t.get('url')==url]
        if len(matches)>1:raise ValueError('有多个 '+symbol+' 官网标签页，请保留一个后重试。')
        opener=build_opener(ProxyHandler({}))
        if not matches:
            with opener.open(Request('http://'+address+'/json/new?'+quote(url,safe=''),method='PUT'),timeout=3) as response:
                page=json.load(response)
            key=page.get('id')
        else:key=matches[0]['id']
        if key:
            with opener.open('http://'+address+'/json/activate/'+quote(key,safe=''),timeout=2) as response:response.read(100)
        return {'success':True,'reused':reused,'symbol':symbol,'orders_submitted':False,
                'message':('已复用现有浏览器中的 '+symbol+' 页面。' if reused else '已打开专用浏览器；首次请在官网自行连接钱包登录。')+
                           ' 登录状态保存在本机；行情监听已连接，收到新报价后显示。不自动开启实盘。'}
