"""Locate the exact market only within a user-configured browser session."""
import re
from urllib.parse import urlsplit


def debugger_status(address, symbol=None):
    """Return a redacted, read-only status for the configured local CDP browser."""
    import json
    from urllib.request import build_opener, ProxyHandler

    result = {
        'address': address,
        'mode': '复用用户启动的本机 Chrome（CDP）',
        'reachable': False,
        'browser': None,
        'pages': [],
        'matching_pages': 0,
        'error': None,
    }
    if not valid_debugger_address(address):
        result['error'] = '调试地址不是仅绑定本机的合法地址'
        return result
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open('http://' + address + '/json/version', timeout=1) as response:
            version = json.load(response)
        with opener.open('http://' + address + '/json/list', timeout=1) as response:
            targets = json.load(response)
    except Exception as exc:
        result['error'] = type(exc).__name__
        return result
    result['reachable'] = True
    result['browser'] = version.get('Browser')
    pages = []
    for target in targets:
        if target.get('type') != 'page':
            continue
        url = target.get('url') or ''
        pages.append({'title': target.get('title') or '', 'url': url})
        if symbol and url == 'https://omni.variational.io/perpetual/' + symbol:
            result['matching_pages'] += 1
    result['pages'] = pages[:20]
    if not pages:
        result['error'] = 'CDP 已连接，但没有打开的网页标签'
    elif symbol and result['matching_pages'] != 1:
        result['error'] = '未找到且仅找到一个目标市场标签页'
    return result


def attach_browser(address):
    """Use a matching installed driver; never download during a live preflight."""
    import json
    import os
    from pathlib import Path
    from urllib.request import build_opener, ProxyHandler
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.remote_connection import ChromeRemoteConnection
    from selenium.webdriver.remote.client_config import ClientConfig
    if not valid_debugger_address(address):
        raise ValueError('仅允许本机浏览器会话')
    opener = build_opener(ProxyHandler({}))
    with opener.open('http://'+address+'/json/list', timeout=2) as response:
        targets=json.load(response)
    if not any(t.get('type')=='page' for t in targets):
        raise ValueError('执行浏览器没有打开页面；请使用控制台准备会话')
    with opener.open('http://'+address+'/json/version', timeout=2) as response:
        browser = json.load(response).get('Browser', '')
    match = re.fullmatch(r'(?:Chrome|HeadlessChrome)/(\d+)\.[\d.]+', browser)
    if not match:
        raise ValueError('无法识别现有 Chrome 版本')
    major = match.group(1)
    driver_root = Path.home()/'.cache/selenium/chromedriver'
    driver_name = 'chromedriver.exe' if os.name == 'nt' else 'chromedriver'
    exact = list(driver_root.glob('*/'+major+'.*/'+driver_name))
    # Chrome auto-updates before Selenium Manager has cached the new driver.
    # A driver from the immediately preceding major is compatible with the
    # current CDP in practice and avoids downloading during a live attach.
    candidates = exact or [p for p in driver_root.glob('*/*/'+driver_name)
                           if os.access(p, os.X_OK)
                           and p.parent.name.split('.', 1)[0].isdigit()
                           and int(p.parent.name.split('.', 1)[0]) < int(major)
                           and int(major) - int(p.parent.name.split('.', 1)[0]) <= 2]
    candidates = [p for p in candidates if os.access(p, os.X_OK)]
    if not candidates:
        raise ValueError('本机缺少匹配 Chrome 的驱动；请先运行执行环境安装后重试')
    path = max(candidates, key=lambda p: tuple(int(v) for v in p.parent.name.split('.')))
    options = webdriver.ChromeOptions()
    options.debugger_address = address
    options.set_capability('goog:loggingPrefs', {'performance': 'ALL'})
    service = Service(str(path))
    service.start()
    try:
        connection=ChromeRemoteConnection(service.service_url,ignore_proxy=True,
            client_config=ClientConfig(remote_server_addr=service.service_url,timeout=8))
        driver=webdriver.Remote(command_executor=connection,options=options)
        driver.service=service
        driver.execute_cdp_cmd=lambda cmd,args: driver.execute('executeCdpCommand',{'cmd':cmd,'params':args})['value']
    except Exception:
        service.stop()
        raise
    driver.set_script_timeout(6)
    driver.set_page_load_timeout(10)
    return driver


def open_empty_market_session(address, symbol):
    """The user-triggered preparation may restore a closed tab, never sign in."""
    import json
    from urllib.request import build_opener, ProxyHandler, Request
    from urllib.parse import quote
    if not valid_debugger_address(address) or symbol not in ('ETH','BTC'):
        raise ValueError('invalid_market_session')
    opener=build_opener(ProxyHandler({}))
    with opener.open('http://'+address+'/json/list',timeout=2) as response:
        targets=json.load(response)
    if not any(t.get('type')=='page' for t in targets):
        url='https://omni.variational.io/perpetual/'+symbol
        request=Request('http://'+address+'/json/new?'+quote(url,safe=''),method='PUT')
        with opener.open(request,timeout=5) as response:
            json.load(response)


def valid_debugger_address(address):
    if not isinstance(address, str):
        return False
    match = re.fullmatch(r'127\.0\.0\.1:(\d{1,5})', address or '')
    return bool(match and 1 <= int(match[1]) <= 65535)


def select_market_tab(driver, symbol):
    if symbol not in ('BTC', 'ETH'):
        raise ValueError('未验收的浏览器市场')
    original = driver.current_window_handle
    matches = []
    try:
        for handle in driver.window_handles:
            driver.switch_to.window(handle)
            url = urlsplit(driver.current_url)
            # A macOS ChromeDriver attach can restore the window when it
            # changes tabs.  Re-minimize immediately while inspecting handles
            # so a background execution does not flash the trading page.
            background_market_window(symbol)
            if (url.scheme == 'https' and url.netloc == 'omni.variational.io'
                    and url.path == '/perpetual/' + symbol and not url.query and not url.fragment):
                matches.append(handle)
        if len(matches) != 1:
            raise ValueError('指定浏览器内须有且仅有一个 Variational '+symbol+' 官网标签页；不会自动登录或切换市场')
        driver.switch_to.window(matches[0])
        background_market_window(symbol)
        return matches[0]
    except Exception:
        driver.switch_to.window(original)
        raise


def background_market_window(symbol):
    """Best-effort hide of the Variational window after an execution attach.

    Chrome's WebDriver ``switch_to.window`` may activate the OS window even
    though all later DOM commands can run against a minimized page.  On macOS
    we minimize only a Chrome window containing the requested Variational URL;
    other platforms simply keep the tab in the background.  This never
    touches browser cookies or the wallet and failure is intentionally ignored
    because it must not affect order safety.
    """
    import subprocess
    import sys
    if sys.platform != 'darwin' or symbol not in ('BTC', 'ETH'):
        return False
    script = f'''tell application "Google Chrome"
repeat with w in windows
  repeat with t in tabs of w
    if URL of t starts with "https://omni.variational.io/perpetual/{symbol}" then
      set miniaturized of w to true
      return true
    end if
  end repeat
end repeat
return false
end tell'''
    try:
        result = subprocess.run(['osascript', '-e', script],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                timeout=2, check=False)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
