"""
Variational API 分析器
从浏览器拦截的请求中分析出可直接调用的 API
"""

import json
import re
from typing import Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from urllib.parse import urlparse, parse_qs
from loguru import logger


@dataclass
class ApiEndpoint:
    """API 端点信息"""
    url: str
    method: str
    headers: Dict
    params: Dict
    sample_body: Dict
    response_fields: Set[str]
    call_count: int = 1
    has_auth: bool = False


class VariationalApiAnalyzer:
    """分析拦截的请求，提取可用 API"""

    def __init__(self):
        self.endpoints: Dict[str, ApiEndpoint] = {}
        self.price_endpoints: List[str] = []
        self.order_endpoints: List[str] = []
        self.auth_tokens: Dict[str, str] = {}

    def analyze_responses(self, responses: List[Dict]) -> Dict:
        """分析拦截的响应列表"""
        for resp in responses:
            self._analyze_single_response(resp)

        return self.generate_api_report()

    def _analyze_single_response(self, resp: Dict):
        """分析单个响应"""
        url = resp.get('url', '')
        method = resp.get('method', 'GET')
        body_str = resp.get('body', '')

        if not url or 'variational' not in url:
            return

        # 解析 URL
        parsed = urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        params = parse_qs(parsed.query)

        # 尝试解析响应体
        try:
            body = json.loads(body_str) if body_str else {}
        except:
            body = {}

        # 提取字段
        fields = set()
        if isinstance(body, dict):
            fields = self._extract_fields(body)
        elif isinstance(body, list) and body:
            if isinstance(body[0], dict):
                fields = self._extract_fields(body[0])

        # 检查是否包含认证信息
        headers = resp.get('headers', {})
        has_auth = any(k.lower() in ['authorization', 'x-auth', 'token', 'jwt']
                      for k in headers.keys())

        # 保存认证token
        for k, v in headers.items():
            if k.lower() in ['authorization', 'x-auth-token']:
                self.auth_tokens[k] = v

        # 更新或创建端点记录
        key = f"{method}:{base_url}"
        if key in self.endpoints:
            self.endpoints[key].call_count += 1
            self.endpoints[key].response_fields.update(fields)
        else:
            self.endpoints[key] = ApiEndpoint(
                url=base_url,
                method=method,
                headers=dict(headers),
                params={k: v[0] if len(v) == 1 else v for k, v in params.items()},
                sample_body=body if isinstance(body, dict) else {},
                response_fields=fields,
                has_auth=has_auth
            )

        # 分类端点
        self._classify_endpoint(key, base_url, fields, body)

    def _extract_fields(self, data: Dict, prefix: str = '') -> Set[str]:
        """递归提取字段名"""
        fields = set()
        for key, value in data.items():
            full_key = f"{prefix}.{key}" if prefix else key
            fields.add(full_key)
            if isinstance(value, dict):
                fields.update(self._extract_fields(value, full_key))
            elif isinstance(value, list) and value and isinstance(value[0], dict):
                fields.update(self._extract_fields(value[0], full_key))
        return fields

    def _classify_endpoint(self, key: str, url: str, fields: Set, body):
        """分类端点用途"""
        url_lower = url.lower()
        fields_lower = {f.lower() for f in fields}

        # 价格相关
        price_indicators = ['price', 'mark', 'index', 'ticker', 'oracle']
        if any(p in url_lower for p in price_indicators):
            if key not in self.price_endpoints:
                self.price_endpoints.append(key)
            return

        # 订单相关
        order_indicators = ['order', 'trade', 'position', 'submit']
        if any(o in url_lower for o in order_indicators):
            if key not in self.order_endpoints:
                self.order_endpoints.append(key)
            return

        # 从字段判断
        if any(f in fields_lower for f in ['price', 'markprice', 'lastprice']):
            if key not in self.price_endpoints:
                self.price_endpoints.append(key)
        elif any(f in fields_lower for f in ['orderid', 'txhash', 'positionid']):
            if key not in self.order_endpoints:
                self.order_endpoints.append(key)

    def generate_api_report(self) -> Dict:
        """生成 API 分析报告"""
        report = {
            'summary': {
                'total_endpoints': len(self.endpoints),
                'price_endpoints': len(self.price_endpoints),
                'order_endpoints': len(self.order_endpoints),
                'auth_tokens_found': len(self.auth_tokens)
            },
            'all_endpoints': {},
            'price_apis': {},
            'order_apis': {},
            'python_code': {}
        }

        # 所有端点
        for key, ep in self.endpoints.items():
            report['all_endpoints'][key] = {
                'url': ep.url,
                'method': ep.method,
                'call_count': ep.call_count,
                'has_auth': ep.has_auth,
                'params': ep.params,
                'response_fields': sorted(list(ep.response_fields))[:20]  # 最多20个字段
            }

        # 价格 API
        for key in self.price_endpoints:
            if key in self.endpoints:
                ep = self.endpoints[key]
                report['price_apis'][key] = {
                    'url': ep.url,
                    'method': ep.method,
                    'params': ep.params,
                    'sample_response': ep.sample_body
                }
                # 生成 Python 代码
                report['python_code'][f'get_price_{key.replace(":", "_")}'] = \
                    self._generate_price_code(ep)

        # 订单 API
        for key in self.order_endpoints:
            if key in self.endpoints:
                ep = self.endpoints[key]
                report['order_apis'][key] = {
                    'url': ep.url,
                    'method': ep.method,
                    'params': ep.params,
                    'sample_response': ep.sample_body
                }

        return report

    def _generate_price_code(self, ep: ApiEndpoint) -> str:
        """生成获取价格的 Python 代码模板"""
        code = f"""import requests

# 从拦截请求分析出的价格 API
# 端点: {ep.url}
# 方法: {ep.method}

def get_price_from_api():
    url = "{ep.url}"
    headers = {{
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        'Accept': 'application/json',
"""
        # 添加认证头
        for k, v in ep.headers.items():
            if k.lower() in ['authorization', 'x-auth-token']:
                code += f"        '{k}': '{v[:20]}...',  # 请替换为实际token\n"

        code += """    }
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        data = response.json()
        
        # 根据实际响应结构调整
        # 可用字段: """
        code += ', '.join(sorted(list(ep.response_fields))[:10])
        code += """
        
        return data
    except Exception as e:
        print(f"请求失败: {e}")
        return None
"""
        return code

    def find_best_price_api(self) -> Optional[ApiEndpoint]:
        """找出最佳价格 API"""
        best = None
        best_score = 0

        for key in self.price_endpoints:
            if key not in self.endpoints:
                continue
            ep = self.endpoints[key]
            score = ep.call_count

            # 优先选择包含 price/markPrice 字段的
            if any('price' in f.lower() for f in ep.response_fields):
                score += 10
            if 'markPrice' in ep.response_fields:
                score += 5

            if score > best_score:
                best_score = score
                best = ep

        return best

    def export_to_python_file(self, filename: str = "variational_api.py"):
        """导出为可直接使用的 Python 文件"""
        best_price_api = self.find_best_price_api()

        code = '''"""
自动生成的 Variational API 调用代码
基于浏览器拦截的请求分析
"""

import requests
import json
from typing import Optional, Dict

# 基础配置
BASE_URL = "https://omni.variational.io"
PROXY = "http://127.0.0.1:7890"  # 如果需要代理

session = requests.Session()
session.proxies = {
    'http': PROXY,
    'https': PROXY
}

'''

        if best_price_api:
            code += f'''
def get_market_price(symbol: str = "BTC") -> Optional[float]:
    """获取市场价格 (自动分析生成)"""
    url = "{best_price_api.url}"
    
    try:
        response = session.get(url, timeout=10)
        data = response.json()
        
        # 尝试提取价格
        if isinstance(data, dict):
            # 常见价格字段
            for key in ['price', 'markPrice', 'indexPrice', 'lastPrice']:
                if key in data:
                    return float(data[key])
        
        return None
    except Exception as e:
        print(f"获取价格失败: {e}")
        return None

'''

        code += '''
def get_all_endpoints() -> Dict:
    """获取所有发现的端点"""
    return {
'''

        for key, ep in self.endpoints.items():
            code += f'''        "{key}": {{
            "url": "{ep.url}",
            "method": "{ep.method}",
            "fields": {sorted(list(ep.response_fields))[:10]}
        }},
'''

        code += '''    }

if __name__ == "__main__":
    # 测试价格获取
    price = get_market_price("BTC")
    print(f"BTC Price: ${{price}}")
'''

        with open(filename, 'w') as f:
            f.write(code)

        logger.info(f"API 代码已导出: {filename}")
        return filename


def analyze_and_save(responses: List[Dict], output_dir: str = ".") -> str:
    """分析响应并保存报告"""
    analyzer = VariationalApiAnalyzer()
    report = analyzer.analyze_responses(responses)

    # 保存 JSON 报告
    import os
    report_file = os.path.join(output_dir, "api_analysis_report.json")
    with open(report_file, 'w') as f:
        json.dump(report, f, indent=2, default=str)

    # 导出 Python 代码
    py_file = analyzer.export_to_python_file(
        os.path.join(output_dir, "variational_api_generated.py")
    )

    logger.info(f"API 分析报告已保存: {report_file}")
    logger.info(f"生成的 Python 代码: {py_file}")

    return report_file