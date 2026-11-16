"""StandX Perps authenticated HTTP transport.

The public dashboard never imports this module.  The isolated execution
environment uses it after a user-triggered credential check.  A POST is only
considered terminal after the order is queried and its status/fill quantity
has been reconciled.
"""
from __future__ import annotations

import base64
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests

from .crypto import base58_encode, generate_seed, public_key, sign


class StandXError(RuntimeError):
    pass


def _json_bytes(value: Dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _jwt_payload(token: str) -> Dict[str, Any]:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        value = json.loads(base64.urlsafe_b64decode(part.encode("ascii")))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (IndexError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise StandXError("StandX signedData 格式无法核对") from exc


@dataclass(frozen=True)
class StandXCredentials:
    wallet_address: str
    wallet_private_key: str
    chain: str = "bsc"

    def normalized_key(self) -> str:
        key = self.wallet_private_key.strip()
        if key.startswith("0x"):
            key = key[2:]
        if len(key) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in key):
            raise StandXError("StandX BSC 钱包私钥必须是 64 位十六进制")
        return "0x" + key


class StandXClient:
    AUTH_URL = "https://api.standx.com"
    BASE_URL = "https://perps.standx.com"

    def __init__(self, credentials: StandXCredentials, session=None,
                 auth_url: Optional[str] = None, base_url: Optional[str] = None,
                 timeout: float = 12.0):
        self.credentials = credentials
        self.auth_url = (auth_url or self.AUTH_URL).rstrip("/")
        self.base_url = (base_url or self.BASE_URL).rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout
        self.token: Optional[str] = None
        self.session_id = str(uuid.uuid4())
        self._request_seed: Optional[bytes] = None

    def close(self):
        """Close the underlying HTTP session without exposing credentials."""
        close = getattr(self.session, "close", None)
        if close:
            close()

    def _post(self, url: str, payload: Dict[str, Any], headers=None):
        try:
            response = self.session.post(url, json=payload, headers=headers or {}, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            raise StandXError("StandX 请求失败") from exc
        except ValueError as exc:
            raise StandXError("StandX 返回格式无效") from exc
        if not isinstance(data, dict):
            raise StandXError("StandX 返回格式无效")
        return data

    def authenticate(self) -> Dict[str, Any]:
        """Perform the official wallet-signature login without placing an order."""
        if self.credentials.chain != "bsc":
            raise StandXError("当前 StandX 执行器先支持 BSC 钱包；Solana 适配待单独验收")
        from eth_account import Account
        from eth_account.messages import encode_defunct

        key = self.credentials.normalized_key()
        account = Account.from_key(key)
        if account.address.lower() != self.credentials.wallet_address.lower():
            raise StandXError("StandX 钱包地址与私钥不匹配")
        seed = generate_seed()
        request_id = base58_encode(public_key(seed))
        prepared = self._post(self.auth_url + "/v1/offchain/prepare-signin?chain=bsc",
                              {"address": account.address, "requestId": request_id})
        signed_data = prepared.get("signedData")
        if not prepared.get("success") or not isinstance(signed_data, str):
            raise StandXError("StandX 登录签名数据获取失败")
        payload = _jwt_payload(signed_data)
        message = payload.get("message")
        if not isinstance(message, str) or not message:
            raise StandXError("StandX 登录消息缺失")
        signature = account.sign_message(encode_defunct(text=message)).signature.hex()
        login = self._post(self.auth_url + "/v1/offchain/login?chain=bsc",
                           {"signature": signature, "signedData": signed_data,
                            "expiresSeconds": 604800})
        token = login.get("token")
        if not isinstance(token, str) or not token:
            raise StandXError("StandX 登录未返回 token")
        if str(login.get("address", account.address)).lower() != account.address.lower():
            raise StandXError("StandX 登录账户与输入钱包不一致")
        self.token = token
        self._request_seed = seed
        return {"authenticated": True, "orders_submitted": False,
                "address": account.address, "chain": login.get("chain", "bsc"),
                "session_id": self.session_id}

    def _ensure_auth(self):
        if not self.token or not self._request_seed:
            self.authenticate()

    def _headers(self, payload: Optional[bytes] = None, signed: bool = False) -> Dict[str, str]:
        self._ensure_auth()
        headers = {"Authorization": "Bearer " + str(self.token),
                   "x-session-id": self.session_id}
        if signed:
            body = payload or b""
            version, request_id, timestamp = "v1", str(uuid.uuid4()), int(time.time() * 1000)
            message = f"{version},{request_id},{timestamp},{body.decode('utf-8')}".encode("utf-8")
            signature = base64.b64encode(sign(message, self._request_seed)).decode("ascii")
            headers.update({"x-request-sign-version": version, "x-request-id": request_id,
                            "x-request-timestamp": str(timestamp),
                            "x-request-signature": signature,
                            "Content-Type": "application/json"})
        return headers

    def _signed_post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        body = _json_bytes(payload)
        try:
            response = self.session.post(self.base_url + path, data=body,
                                         headers=self._headers(body, signed=True), timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            raise StandXError("StandX 订单请求失败") from exc
        except ValueError as exc:
            raise StandXError("StandX 订单返回格式无效") from exc
        if not isinstance(data, dict):
            raise StandXError("StandX 订单返回格式无效")
        if data.get("code") not in (None, 0, "0"):
            raise StandXError("StandX 拒绝订单请求")
        return data

    def _get(self, path: str, params=None, authenticated: bool = False) -> Any:
        headers = self._headers() if authenticated else {}
        try:
            response = self.session.get(self.base_url + path, params=params or {},
                                        headers=headers, timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            raise StandXError("StandX 查询失败") from exc

    @staticmethod
    def symbol_name(symbol: str) -> str:
        value = str(symbol).upper()
        return value if value.endswith("-USD") else value.split("-", 1)[0] + "-USD"

    def query_symbol_market(self, symbol: str) -> Dict[str, Any]:
        data = self._get("/api/query_symbol_market", {"symbol": self.symbol_name(symbol)})
        if not isinstance(data, dict):
            raise StandXError("StandX 行情格式无效")
        return data

    def query_depth_book(self, symbol: str) -> Dict[str, Any]:
        data = self._get("/api/query_depth_book", {"symbol": self.symbol_name(symbol)})
        if not isinstance(data, dict) or not isinstance(data.get("bids"), list) or not isinstance(data.get("asks"), list):
            raise StandXError("StandX 深度格式无效")
        return data

    def query_symbol_info(self, symbol: str) -> Dict[str, Any]:
        data = self._get("/api/query_symbol_info", {"symbol": self.symbol_name(symbol)})
        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            raise StandXError("StandX 合约信息格式无效")
        return data

    def query_balance(self) -> Dict[str, Any]:
        data = self._get("/api/query_balance", authenticated=True)
        if not isinstance(data, dict):
            raise StandXError("StandX 余额格式无效")
        return data

    def query_positions(self, symbol: str) -> list:
        data = self._get("/api/query_positions", {"symbol": self.symbol_name(symbol)}, authenticated=True)
        if not isinstance(data, list):
            raise StandXError("StandX 持仓格式无效")
        return data

    def query_order(self, *, order_id=None, cl_ord_id=None) -> Dict[str, Any]:
        self._ensure_auth()
        params = {}
        if order_id is not None:
            params["order_id"] = order_id
        if cl_ord_id is not None:
            params["cl_ord_id"] = cl_ord_id
        if not params:
            raise ValueError("order_id 或 cl_ord_id 至少填写一个")
        try:
            response = self.session.get(self.base_url + "/api/query_order", params=params,
                                        headers=self._headers(), timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise StandXError("StandX 订单查询失败") from exc
        if not isinstance(data, dict):
            raise StandXError("StandX 订单查询格式无效")
        return data

    def query_trades(self, *, symbol=None, order_id=None, cl_ord_id=None) -> list:
        """Return the user's fills used to reconcile fees after an order."""
        params = {}
        if symbol:
            params["symbol"] = self.symbol_name(symbol)
        if order_id is not None:
            params["order_id"] = order_id
        if cl_ord_id is not None:
            params["cl_ord_id"] = cl_ord_id
        data = self._get("/api/query_trades", params, authenticated=True)
        if isinstance(data, dict):
            data = data.get("result", data.get("trades", []))
        if not isinstance(data, list):
            raise StandXError("StandX 成交明细格式无效")
        return data

    def place_limit_ioc(self, symbol: str, side: str, qty: str, price: str,
                        reduce_only: bool = False, cl_ord_id: Optional[str] = None,
                        reconcile_seconds: float = 8.0) -> Dict[str, Any]:
        if side not in ("buy", "sell"):
            raise ValueError("StandX side 必须是 buy 或 sell")
        client_id = cl_ord_id or uuid.uuid4().hex
        accepted = self._signed_post("/api/new_order", {
            "symbol": symbol, "side": side, "order_type": "limit", "qty": str(qty),
            "price": str(price), "time_in_force": "ioc", "reduce_only": bool(reduce_only),
            "cl_ord_id": client_id,
        })
        deadline = time.monotonic() + reconcile_seconds
        last = accepted
        while time.monotonic() < deadline:
            try:
                last = self.query_order(cl_ord_id=client_id)
            except StandXError:
                time.sleep(0.25)
                continue
            status = str(last.get("status", "")).lower()
            if status in {"filled", "partially_filled", "partial", "canceled", "rejected", "closed"}:
                break
            time.sleep(0.25)
        status = str(last.get("status", "accepted")).lower()
        filled = float(last.get("fill_qty") or last.get("filled_qty") or 0)
        terminal = status in {"filled", "partially_filled", "partial", "canceled", "rejected", "closed"}
        fees, fees_verified = (0.0, True) if filled == 0 and terminal else (None, False)
        if filled > 0 and terminal:
            try:
                trades = self.query_trades(symbol=symbol, order_id=last.get("id") or last.get("order_id"),
                                            cl_ord_id=client_id)
                matching = [row for row in trades
                            if (row.get("order_id") is None or str(row.get("order_id")) == str(last.get("id") or last.get("order_id")))]
                if matching:
                    fees = sum(float(row.get("fee_qty", row.get("fee", 0)) or 0) for row in matching)
                    fees_verified = True
            except (StandXError, TypeError, ValueError):
                pass
        return {"success": filled > 0, "confirmed": terminal,
                "status": "filled" if status == "filled" else "partial" if filled > 0 else status,
                "filled_size": filled, "average_price": last.get("fill_avg_price"),
                "order_id": str(last.get("id") or last.get("order_id") or client_id),
                "fees_usdc": fees, "fees_verified": fees_verified,
                "raw_status": status, "accepted": accepted}

    def cancel_order(self, *, order_id=None, cl_ord_id=None) -> Dict[str, Any]:
        payload = {"order_id": order_id} if order_id is not None else {"cl_ord_id": cl_ord_id}
        if not payload:
            raise ValueError("order_id 或 cl_ord_id 至少填写一个")
        return self._signed_post("/api/cancel_order", payload)
