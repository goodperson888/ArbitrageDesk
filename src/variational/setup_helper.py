"""
Variational浏览器设置助手
帮助用户自动连接钱包并获取BTC价格
"""

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
import time
import json
from loguru import logger


class VariationalSetupHelper:
    """Variational设置助手 (支持代理和BTC)"""

    def __init__(self, headless=False, proxy="http://127.0.0.1:7890"):
        self.headless = headless
        self.proxy = proxy
        self.driver = None
        self.wait = None

        self._setup_browser()

    def _setup_browser(self) -> None:
        """设置浏览器"""
        chrome_options = Options()

        # 无头模式
        if self.headless:
            chrome_options.add_argument('--headless')

        # 代理设置
        if self.proxy:
            chrome_options.add_argument(f'--proxy-server={self.proxy}')
            logger.info(f"已设置代理: {self.proxy}")

        # 其他优化选项
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--window-size=1920,1080')
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        chrome_options.add_argument('--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

        # 禁用自动化检测
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option('useAutomationExtension', False)

        # 初始化驱动
        service = Service(ChromeDriverManager().install())
        self.driver = webdriver.Chrome(service=service, options=chrome_options)

        # 隐藏webdriver标志
        self.driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
            'source': '''
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
            '''
        })

        # 设置等待时间
        self.wait = WebDriverWait(self.driver, 30)
        self.driver.set_page_load_timeout(60)

        logger.info("浏览器初始化完成")

    def start_network_listener(self) -> None:
        """启动网络响应监听器"""
        try:
            script = """
            (function() {
                if (window.__variational_interceptor_installed) return;
                window.__variational_interceptor_installed = true;
                window.__variational_responses = [];

                const originalFetch = window.fetch;
                window.fetch = async function(...args) {
                    const response = await originalFetch.apply(this, args);
                    try {
                        const clone = response.clone();
                        const body = await clone.text();
                        window.__variational_responses.push({
                            url: args[0],
                            method: args[1]?.method || 'GET',
                            status: response.status,
                            body: body,
                            timestamp: new Date().toISOString()
                        });
                        if (window.__variational_responses.length > 100) {
                            window.__variational_responses.shift();
                        }
                    } catch(e) {}
                    return response;
                };

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

    def get_intercepted_responses(self, url_pattern: str = None) -> list:
        """获取拦截到的响应数据"""
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

    def navigate_to_market(self, symbol: str = "BTC") -> bool:
        """导航到指定市场页面"""
        try:
            logger.info(f"正在导航到 {symbol} 市场页面...")

            # 尝试直接访问特定市场页面
            market_url = f"https://omni.variational.io/perpetual/{symbol}"
            self.driver.get(market_url)
            time.sleep(3)

            # 启动网络监听
            self.start_network_listener()

            logger.info(f"成功导航到 {symbol} 市场页面")
            return True

        except Exception as e:
            logger.error(f"导航到市场页面失败: {e}")
            return False

    def get_current_price(self, symbol: str = "BTC") -> float:
        """获取当前市场价格 (优先从API响应)"""
        try:
            # 方法1: 从拦截的响应中获取
            responses = self.get_intercepted_responses()
            for resp in responses:
                try:
                    body = json.loads(resp.get('body', ''))
                    # 递归查找价格
                    price = self._find_price_in_json(body, symbol)
                    if price:
                        logger.info(f"{symbol} 价格(API): ${price:,.2f}")
                        return price
                except:
                    continue

            # 方法2: 从DOM获取
            return self._get_price_from_dom(symbol)

        except Exception as e:
            logger.error(f"获取价格失败: {e}")
            return None

    def _find_price_in_json(self, data, symbol: str):
        """递归在JSON中查找价格"""
        if isinstance(data, dict):
            price_keys = ['price', 'markPrice', 'indexPrice', 'lastPrice', 'currentPrice']
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
            # 递归
            for value in data.values():
                price = self._find_price_in_json(value, symbol)
                if price:
                    return price
        elif isinstance(data, list):
            for item in data:
                price = self._find_price_in_json(item, symbol)
                if price:
                    return price
        return None

    def _get_price_from_dom(self, symbol: str) -> float:
        """从页面DOM获取价格"""
        try:
            price_selectors = [
                "[data-testid='mark-price']",
                "[data-testid='current-price']",
                "[data-testid='price']",
                "[class*='mark-price']",
                "[class*='current-price']",
                "[class*='price-value']",
                "[class*='price']",
                "//span[contains(@class, 'price') and contains(text(), '$')]",
                "//div[contains(@class, 'price') and contains(text(), '$')]",
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
                except:
                    continue

            return None

        except Exception as e:
            logger.error(f"DOM获取价格失败: {e}")
            return None

    def connect_wallet_manual(self) -> bool:
        """手动连接钱包（提供指导）"""
        try:
            logger.info("=== 钱包连接指导 ===")
            logger.info("1. 确保已安装MetaMask或其他钱包插件")
            logger.info("2. 确保钱包已解锁并连接到正确网络")
            logger.info("3. 在浏览器中找到'Connect Wallet'按钮并点击")
            logger.info("4. 在钱包弹窗中授权连接")
            logger.info("5. 等待连接完成")
            logger.info("==================")

            # 导航到主页面
            self.driver.get("https://omni.variational.io")
            time.sleep(3)
            self.start_network_listener()

            # 查找连接按钮并高亮
            connect_selectors = [
                "//button[contains(text(), 'Connect')]",
                "//button[contains(text(), 'Connect Wallet')]",
                "//button[contains(@class, 'connect')]",
                "[class*='connect']"
            ]

            for selector in connect_selectors:
                try:
                    if selector.startswith("//"):
                        elements = self.driver.find_elements(By.XPATH, selector)
                    else:
                        elements = self.driver.find_elements(By.CSS_SELECTOR, selector)

                    for el in elements:
                        if el.is_displayed():
                            # 高亮按钮
                            self.driver.execute_script(
                                "arguments[0].style.border='3px solid red'; arguments[0].style.backgroundColor='yellow';",
                                el
                            )
                            logger.info(f"找到连接按钮，请手动点击")
                            return True
                except:
                    continue

            logger.warning("未能自动找到连接按钮，请手动在页面上查找并点击")
            return True

        except Exception as e:
            logger.error(f"钱包连接指导失败: {e}")
            return False

    def take_screenshot(self, filename: str = "screenshot.png") -> bool:
        """截图"""
        try:
            self.driver.save_screenshot(filename)
            logger.info(f"截图已保存: {filename}")
            return True
        except Exception as e:
            logger.error(f"截图失败: {e}")
            return False

    def close(self) -> None:
        """关闭浏览器"""
        if self.driver:
            self.driver.quit()
            logger.info("浏览器已关闭")


def test_variational_connection():
    """测试Variational连接"""
    print("=== Variational BTC连接测试 ===")

    helper = VariationalSetupHelper(headless=False)

    try:
        # 导航到BTC市场
        symbol = "BTC"
        if helper.navigate_to_market(symbol):
            print(f"✓ 成功导航到{symbol}市场")

            # 等待页面加载
            time.sleep(5)

            # 获取价格
            price = helper.get_current_price(symbol)
            if price:
                print(f"✓ 成功获取价格: ${price:,.2f}")
            else:
                print(f"✗ 未能获取价格，可能需要手动连接钱包")

            # 截图
            helper.take_screenshot(f"variational_{symbol}_test.png")
            print(f"✓ 截图已保存: variational_{symbol}_test.png")

            # 提供手动连接指导
            print("")
            print("=== 手动连接钱包指导 ===")
            helper.connect_wallet_manual()

            print("")
            print("浏览器将保持打开状态，您可以:")
            print("1. 手动连接钱包")
            print("2. 查看页面结构")
            print("3. 测试其他功能")
            print("")
            input("按Enter键关闭浏览器...")

        else:
            print("✗ 导航到市场失败")

    except Exception as e:
        print(f"✗ 测试失败: {e}")
        import traceback
        traceback.print_exc()
    finally:
        helper.close()
        print("测试完成")


if __name__ == "__main__":
    test_variational_connection()