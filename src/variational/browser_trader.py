from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager
import time
import json
import re
from typing import Dict, List, Optional, Tuple, Callable
from dataclasses import dataclass, field
from loguru import logger
from datetime import datetime


@dataclass
class InterceptedResponse:
    """拦截到的响应数据"""
    url: str
    method: str
    status: int
    headers: Dict
    body: Dict
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class MarketData:
    """市场数据"""
    symbol: str
    mark_price: float
    index_price: Optional[float] = None
    funding_rate: Optional[float] = None
    open_interest: Optional[float] = None
    volume_24h: Optional[float] = None
    timestamp: datetime = field(default_factory=datetime.now)


class VariationalBrowserTrader:
    """Variational浏览器自动化交易器 (支持CDP请求拦截)"""

    live_ready = False
    live_blocker = '旧版浏览器下单无法确认单位和成交回执，已禁用自动点击交易'

    def __init__(self, config: Dict, wallet_address: str):
        self.config = config
        self.wallet_address = wallet_address
        self.driver = None
        self.wait = None
        self.is_logged_in = False

        # 拦截数据存储
        self.intercepted_responses: List[InterceptedResponse] = []
        self.market_data_cache: Dict[str, MarketData] = {}
        self.price_callbacks: List[Callable] = []
        self.order_callbacks: List[Callable] = []

        # 代理设置
        self.proxy = config.get('browser', {}).get('proxy', 'http://127.0.0.1:7890')

        self._setup_browser()
        self._setup_cdp_interception()

    def _setup_browser(self) -> None:
        """设置浏览器 (带代理+反检测)"""
        chrome_options = Options()

        # 无头模式 - 建议False以避免检测
        if self.config.get('browser', {}).get('headless', False):
            chrome_options.add_argument('--headless')
            # 无头模式下需要额外参数
            chrome_options.add_argument('--headless=new')  # 新版无头模式

        # 代理设置
        if self.proxy:
            chrome_options.add_argument(f'--proxy-server={self.proxy}')
            logger.info(f"已设置代理: {self.proxy}")

        # 反检测核心参数
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--window-size=1920,1080')
        chrome_options.add_argument('--start-maximized')
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')

        # 禁用自动化检测标志
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
        chrome_options.add_experimental_option('useAutomationExtension', False)

        # 添加更多真实浏览器特征
        chrome_options.add_argument('--disable-infobars')
        chrome_options.add_argument('--disable-extensions')
        chrome_options.add_argument('--disable-popup-blocking')
        chrome_options.add_argument('--disable-notifications')
        chrome_options.add_argument('--disable-default-apps')
        chrome_options.add_argument('--disable-background-timer-throttling')
        chrome_options.add_argument('--disable-backgrounding-occluded-windows')
        chrome_options.add_argument('--disable-renderer-backgrounding')
        chrome_options.add_argument('--disable-features=TranslateUI')
        chrome_options.add_argument('--disable-component-extensions-with-background-pages')

        # 设置用户数据目录 (使用真实Chrome配置)
        user_data_dir = self.config.get('browser', {}).get('user_data_dir')
        if user_data_dir:
            chrome_options.add_argument(f'--user-data-dir={user_data_dir}')
            logger.info(f"使用用户数据目录: {user_data_dir}")

        # 初始化驱动
        service = Service(ChromeDriverManager().install())
        self.driver = webdriver.Chrome(service=service, options=chrome_options)

        # 执行CDP命令隐藏webdriver标志 (增强版)
        self.driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
            'source': '''
                // 隐藏 webdriver 标志
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });

                // 模拟插件
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [
                        {name: "Chrome PDF Plugin", filename: "internal-pdf-viewer"},
                        {name: "Chrome PDF Viewer", filename: "mhjfbmdgcfjbbpaeojofohoefgiehjai"},
                        {name: "Native Client", filename: "internal-nacl-plugin"}
                    ]
                });

                // 模拟语言
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['zh-CN', 'zh', 'en-US', 'en']
                });

                // 模拟权限
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                        Promise.resolve({ state: Notification.permission }) :
                        originalQuery(parameters)
                );

                // 覆盖 chrome 对象
                window.chrome = {
                    runtime: {
                        OnInstalledReason: {CHROME_UPDATE: "chrome_update"},
                        OnRestartRequiredReason: {APP_UPDATE: "app_update"},
                        PlatformArch: {ARM: "arm"},
                        PlatformNaclArch: {ARM: "arm"},
                        PlatformOs: {ANDROID: "android"},
                        RequestUpdateCheckStatus: {NO_UPDATE: "no_update"},
                        updateAvailable: true
                    }
                };

                // 模拟 Notification
                if (!window.Notification) {
                    window.Notification = {
                        permission: "default"
                    };
                }

                // 覆盖 canvas fingerprint
                const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
                HTMLCanvasElement.prototype.toDataURL = function(type) {
                    if (type === 'image/png' && this.width === 220 && this.height === 30) {
                        return 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAANwAAAAeCAYAAABskXulAAAABmJLR0QA/wD/AP+gvaeTAAAACXBIWXMAAAsTAAALEwEAmpwYAAAAB3RJTUUH4wYSEhEfL4jRJwAAAB1pVFh0Q29tbWVudAAAAAAAQ3JlYXRlZCB3aXRoIEdJTVBkLmUHAAAAEklEQVjY2NgwQAxG';
                    }
                    return originalToDataURL.apply(this, arguments);
                };
            '''
        })

        # 设置更真实的 User-Agent
        self.driver.execute_cdp_cmd('Network.setUserAgentOverride', {
            'userAgent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'acceptLanguage': 'zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7',
            'platform': 'MacIntel'
        })

        # 设置等待时间
        timeout = self.config.get('browser', {}).get('timeout', 30)
        page_load_timeout = self.config.get('browser', {}).get('page_load_timeout', 60)
        self.wait = WebDriverWait(self.driver, timeout)
        self.driver.set_page_load_timeout(page_load_timeout)

        logger.info("浏览器初始化完成 (带代理和CDP拦截)")

    def _setup_cdp_interception(self) -> None:
        """设置CDP网络请求拦截"""
        try:
            # 启用网络跟踪
            self.driver.execute_cdp_cmd('Network.enable', {})

            # 添加响应拦截监听
            self.driver.execute_cdp_cmd('Network.setRequestInterception', {
                'patterns': [
                    {'urlPattern': '*', 'resourceType': 'XHR', 'interceptionStage': 'HeadersReceived'},
                    {'urlPattern': '*', 'resourceType': 'Fetch', 'interceptionStage': 'HeadersReceived'}
                ]
            })

            logger.info("CDP网络拦截已启用")
        except Exception as e:
            logger.warning(f"CDP拦截设置失败 (不影响基本功能): {e}")

    def start_network_listener(self) -> None:
        """启动网络响应监听器 (在页面加载后调用)"""
        try:
            # 通过执行JS来监听fetch和XHR
            script = """
            (function() {
                if (window.__variational_interceptor_installed) return;
                window.__variational_interceptor_installed = true;
                window.__variational_responses = [];

                // 拦截 fetch
                const originalFetch = window.fetch;
                window.fetch = async function(...args) {
                    const response = await originalFetch.apply(this, args);
                    const url = args[0];
                    try {
                        const clone = response.clone();
                        const body = await clone.text();
                        window.__variational_responses.push({
                            url: url,
                            method: args[1]?.method || 'GET',
                            status: response.status,
                            headers: Object.fromEntries(response.headers.entries()),
                            body: body,
                            timestamp: new Date().toISOString()
                        });
                        // 保持数组大小
                        if (window.__variational_responses.length > 100) {
                            window.__variational_responses.shift();
                        }
                    } catch(e) {}
                    return response;
                };

                // 拦截 XMLHttpRequest
                const originalXHROpen = XMLHttpRequest.prototype.open;
                const originalXHRSend = XMLHttpRequest.prototype.send;

                XMLHttpRequest.prototype.open = function(method, url, ...rest) {
                    this._url = url;
                    this._method = method;
                    return originalXHROpen.apply(this, [method, url, ...rest]);
                };

                XMLHttpRequest.prototype.send = function(body) {
                    this.addEventListener('load', function() {
                        try {
                            window.__variational_responses.push({
                                url: this._url,
                                method: this._method,
                                status: this.status,
                                headers: {},
                                body: this.responseText,
                                timestamp: new Date().toISOString()
                            });
                            if (window.__variational_responses.length > 100) {
                                window.__variational_responses.shift();
                            }
                        } catch(e) {}
                    });
                    return originalXHRSend.apply(this, [body]);
                };

                return 'Interceptor installed';
            })();
            """
            result = self.driver.execute_script(script)
            logger.info(f"网络监听器已启动: {result}")
        except Exception as e:
            logger.error(f"启动网络监听器失败: {e}")

    def get_intercepted_responses(self, url_pattern: str = None) -> List[Dict]:
        """获取拦截到的响应数据
        Args:
            url_pattern: 可选的URL过滤模式 (如 'price', 'market', 'order')
        """
        try:
            responses = self.driver.execute_script(
                "return window.__variational_responses || [];"
            )

            if url_pattern:
                responses = [
                    r for r in responses
                    if url_pattern.lower() in r.get('url', '').lower()
                ]

            return responses
        except Exception as e:
            logger.error(f"获取拦截响应失败: {e}")
            return []

    def extract_price_from_responses(self, symbol: str = "BTC") -> Optional[float]:
        """从拦截的响应中提取价格数据"""
        responses = self.get_intercepted_responses()

        for resp in responses:
            try:
                body = resp.get('body', '')
                if not body:
                    continue

                # 尝试解析JSON
                data = json.loads(body)

                # 递归查找价格字段
                price = self._find_price_in_data(data, symbol)
                if price:
                    return price

            except (json.JSONDecodeError, Exception):
                # 尝试从文本中提取价格
                price = self._extract_price_from_text(body, symbol)
                if price:
                    return price

        return None

    def _find_price_in_data(self, data, symbol: str) -> Optional[float]:
        """递归在JSON数据中查找价格"""
        if isinstance(data, dict):
            # 常见价格字段名
            price_keys = ['price', 'markPrice', 'indexPrice', 'lastPrice',
                         'currentPrice', 'spotPrice', 'oraclePrice']

            for key in price_keys:
                if key in data:
                    val = data[key]
                    if isinstance(val, (int, float)) and val > 0:
                        return float(val)

            # 检查symbol匹配
            if 'symbol' in data and symbol in str(data['symbol']):
                for key in price_keys:
                    if key in data:
                        val = data[key]
                        if isinstance(val, (int, float)) and val > 0:
                            return float(val)

            # 递归搜索
            for value in data.values():
                price = self._find_price_in_data(value, symbol)
                if price:
                    return price

        elif isinstance(data, list):
            for item in data:
                price = self._find_price_in_data(item, symbol)
                if price:
                    return price

        return None

    def _extract_price_from_text(self, text: str, symbol: str) -> Optional[float]:
        """从文本中提取价格"""
        # 匹配 $XX,XXX.XX 或 XX,XXX.XX 格式
        patterns = [
            r'\$?([0-9,]+\.?\d*)',
            r'price["\']?\s*[:=]\s*["\']?([0-9,]+\.?\d*)',
        ]

        for pattern in patterns:
            matches = re.findall(pattern, text)
            for match in matches:
                try:
                    price = float(match.replace(',', ''))
                    if 1000 < price < 200000:  # BTC合理价格范围
                        return price
                except ValueError:
                    continue

        return None

    def connect_wallet(self) -> bool:
        """连接钱包 (手动模式，提供指导)"""
        try:
            logger.info("开始连接钱包...")

            # 访问Variational
            self.driver.get("https://omni.variational.io")
            time.sleep(3)

            # 启动网络监听
            self.start_network_listener()

            # 查找并点击连接钱包按钮
            connect_selectors = [
                "//button[contains(text(), 'Connect')]",
                "//button[contains(text(), 'Connect Wallet')]",
                "//button[contains(@class, 'connect')]",
                "//div[contains(text(), 'Connect')]",
                "//span[contains(text(), 'Connect')]",
            ]

            connect_button = None
            for selector in connect_selectors:
                try:
                    elements = self.driver.find_elements(By.XPATH, selector)
                    for el in elements:
                        if el.is_displayed() and el.is_enabled():
                            connect_button = el
                            break
                    if connect_button:
                        break
                except:
                    continue

            if connect_button:
                logger.info("找到连接钱包按钮，请点击完成连接")
                # 高亮按钮以便用户找到
                self.driver.execute_script(
                    "arguments[0].style.border='3px solid red'; arguments[0].style.backgroundColor='yellow';",
                    connect_button
                )
            else:
                logger.info("请在浏览器中手动点击'Connect Wallet'按钮")

            # 等待用户手动连接
            logger.info("请在浏览器中完成钱包连接，然后按Enter继续...")
            input("按Enter确认已连接钱包...")

            # 验证是否成功连接
            self.is_logged_in = self._check_login_status()

            if self.is_logged_in:
                logger.info("钱包连接成功")
                return True
            else:
                logger.error("钱包连接失败或未检测到")
                return False

        except Exception as e:
            logger.error(f"连接钱包时出错: {e}")
            return False

    def _check_login_status(self) -> bool:
        """检查登录状态"""
        try:
            # 检查页面是否显示钱包地址或已登录状态
            # 多种可能的选择器
            selectors = [
                "//span[contains(@class, 'address')]",
                "//span[contains(text(), '0x')]",
                "//div[contains(@class, 'wallet')]",
                "//button[contains(@class, 'account')]",
            ]

            for selector in selectors:
                try:
                    elements = self.driver.find_elements(By.XPATH, selector)
                    for el in elements:
                        text = el.text.strip()
                        if text and ('0x' in text or len(text) > 10):
                            return True
                except:
                    continue

            # 检查URL变化
            current_url = self.driver.current_url
            if '/portfolio' in current_url or '/trade' in current_url:
                return True

            return False
        except Exception:
            return False

    def navigate_to_market(self, symbol: str = "BTC") -> bool:
        """导航到指定市场页面"""
        try:
            market_url = f"https://omni.variational.io/perpetual/{symbol}"
            logger.info(f"正在导航到 {symbol} 市场: {market_url}")

            self.driver.get(market_url)
            time.sleep(3)

            # 启动网络监听
            self.start_network_listener()

            # 等待页面加载
            time.sleep(2)

            logger.info(f"成功导航到 {symbol} 市场")
            return True

        except Exception as e:
            logger.error(f"导航到市场页面失败: {e}")
            return False

    def get_market_price(self, symbol: str = "BTC") -> Optional[float]:
        from src.variational.public_client import VariationalPublicClient
        if not hasattr(self, '_public_client'):
            self._public_client = VariationalPublicClient(self.config)
        return self._public_client.get_market_price(symbol)

    def _get_price_from_dom(self, symbol: str) -> Optional[float]:
        """从页面DOM元素中获取价格"""
        try:
            # 常见价格选择器 (按优先级排序)
            price_selectors = [
                # 特定数据属性
                "[data-testid='mark-price']",
                "[data-testid='current-price']",
                "[data-testid='price']",
                # 类名包含price
                "[class*='mark-price']",
                "[class*='current-price']",
                "[class*='price-value']",
                "[class*='price']",
                # XPath
                "//span[contains(@class, 'price') and contains(text(), '$')]",
                "//div[contains(@class, 'price') and contains(text(), '$')]",
                "//span[contains(text(), 'Mark')]/following-sibling::*",
                "//span[contains(text(), 'Price')]/following-sibling::*",
                # 通用
                "//h1[contains(text(), '$')]",
                "//h2[contains(text(), '$')]",
                "//h3[contains(text(), '$')]",
            ]

            for selector in price_selectors:
                try:
                    if selector.startswith("//"):
                        elements = self.driver.find_elements(By.XPATH, selector)
                    else:
                        elements = self.driver.find_elements(By.CSS_SELECTOR, selector)

                    for element in elements:
                        text = element.text.strip()
                        if text and '$' in text:
                            price_text = text.replace('$', '').replace(',', '').strip()
                            try:
                                price = float(price_text)
                                if price > 0:
                                    logger.info(f"{symbol} 价格(DOM): ${price:,.2f}")
                                    return price
                            except ValueError:
                                continue
                except Exception:
                    continue

            return None

        except Exception as e:
            logger.error(f"DOM获取价格失败: {e}")
            return None

    def place_order(self, *args, **kwargs) -> Dict:
        # The old code used broad selectors and assumed success without a fill.
        # Do not restore clicks until exact amount units and receipts are verified.
        return {'success': False, 'status': 'rejected', 'confirmed': True,
                'filled_size': 0, 'error': self.live_blocker}

    def _find_tx_hash(self, data) -> Optional[str]:
        """在响应数据中查找交易哈希"""
        if isinstance(data, dict):
            # 常见交易哈希字段
            tx_keys = ['txHash', 'transactionHash', 'hash', 'tx_hash', 'txnHash']
            for key in tx_keys:
                if key in data:
                    val = data[key]
                    if val and isinstance(val, str) and len(val) >= 10:
                        return val

            # 递归搜索
            for value in data.values():
                tx_hash = self._find_tx_hash(value)
                if tx_hash:
                    return tx_hash

        elif isinstance(data, list):
            for item in data:
                tx_hash = self._find_tx_hash(item)
                if tx_hash:
                    return tx_hash

        return None

    def close_position(self, *args, **kwargs) -> Dict:
        return {'success': False, 'status': 'rejected', 'confirmed': True,
                'filled_size': 0, 'error': self.live_blocker}

    def get_positions(self) -> List[Dict]:
        """获取当前持仓"""
        try:
            # 尝试从页面获取持仓信息
            self.driver.get("https://omni.variational.io/portfolio")
            time.sleep(3)
            self.start_network_listener()
            time.sleep(2)

            positions = []

            # 从拦截的响应中获取持仓数据
            responses = self.get_intercepted_responses()
            for resp in responses:
                try:
                    body = json.loads(resp.get('body', '{}'))
                    # 查找持仓数据
                    pos_data = self._find_positions_in_data(body)
                    if pos_data:
                        positions.extend(pos_data)
                except:
                    continue

            if positions:
                return positions

            # 从DOM获取
            position_rows = self.driver.find_elements(By.CSS_SELECTOR, "[class*='position']")
            for row in position_rows:
                try:
                    cells = row.find_elements(By.CSS_SELECTOR, "td, div")
                    if len(cells) >= 3:
                        positions.append({
                            'symbol': cells[0].text.strip(),
                            'side': cells[1].text.strip(),
                            'size': cells[2].text.strip(),
                        })
                except:
                    continue

            return positions

        except Exception as e:
            logger.error(f"获取持仓失败: {e}")
            return []

    def _find_positions_in_data(self, data) -> List[Dict]:
        """在数据中查找持仓信息"""
        positions = []

        if isinstance(data, dict):
            # 常见持仓字段
            pos_keys = ['positions', 'activePositions', 'openPositions', 'trades']
            for key in pos_keys:
                if key in data and isinstance(data[key], list):
                    for pos in data[key]:
                        if isinstance(pos, dict):
                            positions.append(pos)

            # 递归搜索
            for value in data.values():
                if isinstance(value, (dict, list)):
                    positions.extend(self._find_positions_in_data(value))

        elif isinstance(data, list):
            for item in data:
                positions.extend(self._find_positions_in_data(item))

        return positions

    def take_screenshot(self, filename: str = None) -> str:
        """截图"""
        if filename is None:
            filename = f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"

        try:
            self.driver.save_screenshot(filename)
            logger.info(f"截图已保存: {filename}")
            return filename
        except Exception as e:
            logger.error(f"截图失败: {e}")
            return None

    def get_page_source(self) -> str:
        """获取页面源码 (用于调试)"""
        return self.driver.page_source

    def execute_script(self, script: str) -> any:
        """执行JavaScript"""
        return self.driver.execute_script(script)

    def close(self) -> None:
        """关闭浏览器"""
        if self.driver:
            self.driver.quit()
            logger.info("浏览器已关闭")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()