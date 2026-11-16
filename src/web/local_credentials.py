"""Local-only secret persistence and isolated, read-only SDK verification."""
from src.common.platform_support import execution_python, private_fchmod
import json
import os
import re
import subprocess
import tempfile
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from dotenv import dotenv_values
from src.lighter.account_check import MESSAGES


NAMES = ('LIGHTER_ACCOUNT_INDEX', 'LIGHTER_API_KEY_INDEX', 'LIGHTER_API_PRIVATE_KEY')
RH_NAMES = ('LIGHTER_RH_ACCOUNT_INDEX', 'LIGHTER_RH_API_KEY_INDEX', 'LIGHTER_RH_API_PRIVATE_KEY')
STANDX_NAMES = ('STANDX_WALLET_ADDRESS', 'STANDX_WALLET_PRIVATE_KEY', 'STANDX_CHAIN')
ARCUS_NAMES = ('ARCUS_WALLET_ADDRESS', 'ARCUS_ACCOUNT_INDEX', 'ARCUS_API_SIGNING_KEY')


def names_for(venue='lighter'):
    if venue == 'lighter_rh':
        return RH_NAMES
    if venue == 'standx':
        return STANDX_NAMES
    if venue == 'arcus':
        return ARCUS_NAMES
    return NAMES


def bind_fee_verification(profile_path, values, report):
    """Persist only verified fee evidence, without enabling any execution gate."""
    if report.get('authenticated') is not True or report.get('orders_submitted') is not False:
        raise CredentialError('缺少有效的只读鉴权结果')
    path = Path(profile_path)
    if path.is_symlink():
        raise CredentialError('执行配置不能是符号链接')
    profile = json.loads(path.read_text())
    profile.update(lighter_fee_verified=report.get('taker_fee_bps') == 0,
                   lighter_fee_bps=report.get('taker_fee_bps'),
                   lighter_fee_checked_at=report.get('checked_at'),
                   lighter_fee_account_index=values[NAMES[0]],
                   lighter_fee_key_index=values[NAMES[1]],
                   lighter_fee_key_digest=hashlib.sha256(values[NAMES[2]].encode()).hexdigest())
    fd, temporary = tempfile.mkstemp(prefix='.fee-evidence-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(profile, out, ensure_ascii=False, indent=2, allow_nan=False)
            out.flush(); os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class CredentialError(ValueError):
    """Only these fixed messages may be returned to the webpage."""


def validate(payload, venue='lighter'):
    if venue == 'arcus':
        if not isinstance(payload, dict) or set(payload) != {'wallet_address', 'account_index', 'api_signing_key'}:
            raise CredentialError('请填写 Arcus 钱包地址、账户索引和 API Signing Key')
        address = payload['wallet_address']
        account = payload['account_index']
        key = payload['api_signing_key']
        if not isinstance(address, str) or not address.strip() or len(address.strip()) > 128:
            raise CredentialError('Arcus 钱包地址格式无效')
        if type(account) is not int or account < 0:
            raise CredentialError('Arcus 账户索引须为非负整数')
        if not isinstance(key, str) or not re.fullmatch(r'(?:0x)?[0-9a-fA-F]{64}', key.strip()):
            raise CredentialError('Arcus API Signing Key 需要 64 位十六进制私钥；不要填钱包私钥')
        return {'ARCUS_WALLET_ADDRESS': address.strip(), 'ARCUS_ACCOUNT_INDEX': str(account),
                'ARCUS_API_SIGNING_KEY': key.strip().removeprefix('0x')}
    if venue == 'standx':
        if not isinstance(payload, dict) or set(payload) != {'wallet_address', 'wallet_private_key', 'chain'}:
            raise CredentialError('请填写 StandX 钱包地址、钱包私钥和链')
        address, key, chain = payload['wallet_address'], payload['wallet_private_key'], payload['chain']
        if not isinstance(address, str) or not re.fullmatch(r'0x[0-9a-fA-F]{40}', address.strip()):
            raise CredentialError('StandX BSC 钱包地址格式无效')
        if not isinstance(key, str) or not re.fullmatch(r'(?:0x)?[0-9a-fA-F]{64}', key.strip()):
            raise CredentialError('StandX BSC 钱包私钥需要 64 位十六进制；不要填助记词')
        if chain != 'bsc':
            raise CredentialError('当前 StandX 执行器只接受 bsc')
        return {'STANDX_WALLET_ADDRESS': address.strip(),
                'STANDX_WALLET_PRIVATE_KEY': key.strip().removeprefix('0x'),
                'STANDX_CHAIN': chain}
    if not isinstance(payload, dict) or set(payload) != {'account_index', 'key_index', 'private_key'}:
        raise CredentialError('请填写账户索引、API key 索引和 API 私钥')
    account, index = payload['account_index'], payload['key_index']
    if type(account) is not int or not 0 <= account < 2**48 or type(index) is not int or not 4 <= index <= 254:
        raise CredentialError('账户索引须为非负整数，API key 索引须为 4–254')
    key = payload['private_key']
    if not isinstance(key, str) or not re.fullmatch(r'(?:0x)?[0-9a-fA-F]{80}', key.strip()):
        raise CredentialError('需要 80 位十六进制 Lighter API 私钥；不要填钱包私钥或公钥')
    target = names_for(venue)
    return dict(zip(target, (str(account), str(index), key.strip().removeprefix('0x'))))


def status(path, venue='lighter'):
    values = dotenv_values(path) if path.exists() else {}
    names = names_for(venue)
    pattern = r'[0-9a-fA-F]{80}' if venue not in ('standx', 'arcus') else r'[0-9a-fA-F]{64}'
    key_name = names[2] if venue not in ('standx',) else names[1]
    configured = bool(re.fullmatch(pattern, values.get(key_name) or ''))
    return {'saved': configured, 'account_index': values.get(names[1] if venue == 'arcus' else names[0]) if configured and venue != 'standx' else None,
            'key_index': values.get(names[1]) if configured and venue not in ('standx', 'arcus') else None,
            'wallet_address': values.get(names[0]) if configured and venue in ('standx', 'arcus') else None,
            'authenticated': False, 'live_enabled': False}


def save(path, values, venue='lighter'):
    """Preserve unrelated settings, replace all credential duplicates atomically."""
    path = Path(path)
    if path.is_symlink():
        raise CredentialError('密钥文件不能是符号链接')
    old = path.read_text() if path.exists() else ''
    names = names_for(venue)
    pattern = re.compile(r'^\s*(?:export\s+)?(?:' + '|'.join(names) + r')\s*=')
    lines = [line for line in old.splitlines() if not pattern.match(line)]
    content = '\n'.join(lines + [f"{name}='{values[name]}'" for name in names]) + '\n'
    fd, temporary = tempfile.mkstemp(prefix='.credentials-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            private_fchmod(out.fileno())
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def verify(root, values, proxy, venue='lighter', arcus_base_url=None):
    if venue == 'arcus':
        return verify_arcus(values, proxy, base_url=arcus_base_url)
    python = execution_python(root)
    if not python.exists():
        raise CredentialError('缺少执行环境 .venv-execution；密钥未保存')
    # Secret only traverses stdin, never process arguments or captured output.
    verifier = 'verify_standx_stdin.py' if venue == 'standx' else 'verify_lighter_stdin.py'
    try:
        result = subprocess.run([str(python), '-B', str(root / 'scripts' / verifier)],
            input=json.dumps({'credentials': values, 'proxy': proxy, 'venue': venue}), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=45)
    except subprocess.TimeoutExpired:
        raise CredentialError(MESSAGES[26]+'；密钥未保存') from None
    if result.returncode:
        message = {30: 'StandX 钱包鉴权失败', 31: 'StandX 鉴权出现未分类错误'}.get(
            result.returncode, MESSAGES.get(result.returncode, MESSAGES[29]))
        raise CredentialError(message+'；密钥未保存')
    try:
        report = json.loads(result.stdout.splitlines()[-1])['read_only_result']
        if report.get('authenticated') is not True or report.get('orders_submitted') is not False:
            raise ValueError()
        return report
    except (ValueError, KeyError, IndexError, TypeError):
        raise CredentialError('只读验证输出无法核对，密钥未保存') from None


def verify_arcus(values, proxy=None, base_url=None):
    """Verify Arcus credentials and bind the key to its subaccount.

    The private signing key is used locally to derive the public API key and
    is never returned or logged.  The registered-key lookup is deliberately
    performed before the account lookup so a wrong key, wallet, or subaccount
    produces a useful diagnostic instead of the generic Arcus 404 message.
    """
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
        private = ed25519.Ed25519PrivateKey.from_private_bytes(
            bytes.fromhex(values['ARCUS_API_SIGNING_KEY']))
        public = private.public_key().public_bytes_raw().hex()
    except Exception:
        raise CredentialError('Arcus API Signing Key 格式不正确') from None
    wallet = values['ARCUS_WALLET_ADDRESS']
    account_index = int(values['ARCUS_ACCOUNT_INDEX'])
    base = str(base_url or 'https://api.arcus.xyz').strip().rstrip('/')
    if not re.fullmatch(r'https://[^/]+', base):
        raise CredentialError('Arcus API 地址配置无效')
    try:
        import httpx
        with httpx.Client(timeout=10.0, proxy=proxy) as client:
            headers = {'X-API-Key': public, 'Content-Type': 'application/json'}

            # Match the locally derived public key against Arcus' registered
            # keys before querying the account.  This also confirms that the
            # selected API key belongs to the configured Subaccount #.
            keys_response = client.get(f'{base}/v1/apiKeys', params={'address': wallet}, headers=headers)
            if keys_response.status_code in (401, 403):
                raise CredentialError('Arcus API Key 未授权，或钱包地址不在 API 白名单')
            if keys_response.status_code == 404:
                raise CredentialError('Arcus 找不到该钱包的 API Key 列表，请确认钱包地址和网络')
            keys_response.raise_for_status()
            keys_payload = keys_response.json() if keys_response.content else {}
            keys = keys_payload.get('apiKeys', []) if isinstance(keys_payload, dict) else []
            if not isinstance(keys, list):
                keys = []
            match = next((item for item in keys
                          if isinstance(item, dict)
                          and str(item.get('apiKey', '')).lower() == public.lower()), None)
            if match is None:
                if not keys:
                    raise CredentialError('Arcus 该钱包没有已注册 API Key，请先在 API Keys 页面授权')
                raise CredentialError('Arcus API Signing Key 与该钱包已注册 Key 不匹配；请使用对应 Subaccount 的私钥')
            registered_index = match.get('accountIndex')
            try:
                registered_index = int(registered_index)
            except (TypeError, ValueError):
                registered_index = None
            if registered_index is None or registered_index != account_index:
                raise CredentialError(
                    f'Arcus API Key 属于 Subaccount #{registered_index if registered_index is not None else "未知"}，'
                    f'当前账户索引填的是 #{account_index}；请保持两者一致')
            if str(match.get('status', '')).upper() not in ('', 'ACTIVE'):
                raise CredentialError(f"Arcus API Key 当前状态为 {match.get('status')}，请重新授权")

            account_response = client.get(
                f'{base}/v1/account',
                params={'address': wallet, 'accountIndex': account_index},
                headers=headers,
            )
            if account_response.status_code == 404:
                raise CredentialError(
                    f'Arcus 找到 API Key（Subaccount #{account_index}），但账户接口返回 404；'
                    '该子账户可能尚未入金或尚未初始化，请先在 Arcus 核对账户')
            if account_response.status_code in (401, 403):
                raise CredentialError('Arcus API Key 未授权，或钱包地址不在 API 白名单')
            account_response.raise_for_status()
            account = account_response.json() if account_response.content else {}
            if isinstance(account, dict) and isinstance(account.get('data'), dict):
                account = account['data']
            # The exchange can expose either a direct account rate or a tier
            # reference.  Read the public schedule as evidence, never assume
            # zero when the account response omits the field.
            fee_bps = _arcus_taker_fee_bps(account)
            if fee_bps is None:
                try:
                    tiers_response = client.get(f'{base}/v1/feetiers')
                    tiers_response.raise_for_status()
                    fee_bps = _arcus_taker_fee_bps(account, tiers_response.json())
                except Exception:
                    fee_bps = None
        key_label = str(match.get('apiWalletName') or '').strip()
        binding = f"API Key{('「' + key_label + '」') if key_label else ''} · Subaccount #{account_index}"
        return {
            'authenticated': True, 'orders_submitted': False,
            'checked_at': datetime.now(timezone.utc).isoformat(),
            'account_index': account_index,
            'registered_account_index': registered_index,
            'api_key_match': True,
            'api_key_status': match.get('status'),
            'api_key_name': match.get('apiWalletName'),
            'api_key_valid_until': match.get('validUntil'),
            'api_base_url': base,
            'equity': float(account.get('equity', 0) or 0),
            'free_collateral': float(account.get('freeCollateral', 0) or 0),
            'taker_fee_bps': fee_bps,
            'fee_verified': isinstance(fee_bps, (int, float)),
            'message': (f'Arcus 只读鉴权通过；{binding}；账户 Taker 费率已核验（{fee_bps:.2f} bps），可进入实盘检查'
                        if isinstance(fee_bps, (int, float))
                        else f'Arcus 只读鉴权通过；{binding}；账户 Taker 费率未核验，暂不能实盘'),
        }
    except CredentialError:
        raise
    except Exception as exc:
        text = str(exc)
        if '401' in text or '403' in text:
            raise CredentialError('Arcus API Key 未授权或与钱包地址不匹配') from None
        if '404' in text:
            raise CredentialError('Arcus 找不到该钱包或账户索引，请确认钱包地址、Subaccount # 和网络') from None
        raise CredentialError('Arcus 鉴权请求失败，请检查网络/本地代理') from None


def _arcus_taker_fee_bps(account, schedule=None):
    """Extract a fee rate from the flexible Arcus account/tier shapes.

    Rates may be returned as basis points (``*_bps``), fractions (0.0005),
    or percentages (0.05).  Tier lookup is deliberately conservative: it
    only returns a value when the account tier can be matched unambiguously.
    """
    import math

    def rate(row):
        if not isinstance(row, dict):
            return None
        for key, value in row.items():
            key_low = str(key).lower().replace('-', '_')
            if 'taker' not in key_low or 'fee' not in key_low:
                continue
            try:
                num = float(value)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(num) or num < 0:
                continue
            if 'bps' in key_low or 'basis' in key_low:
                return num
            if 'ppm' in key_low:
                # Arcus publishes fee tiers in parts per million: 450 ppm
                # equals 4.5 bps.
                return num / 100.0
            # A percentage such as 0.05 means 5 bps; a fraction such as
            # 0.0005 also means 5 bps.  Values above one are already bps.
            return num * 100 if num <= 1 else num
        return None

    direct = rate(account)
    if direct is not None:
        return direct
    if not isinstance(schedule, dict) or not isinstance(account, dict):
        return None
    tier_id = None
    for key in ('feeTier', 'feeTierId', 'fee_tier', 'fee_tier_id',
                'feeTierLevel', 'fee_tier_level', 'tier', 'tierId'):
        value = account.get(key)
        if isinstance(value, dict):
            tier_id = value.get('id') or value.get('tierId') or value.get('name')
            found = rate(value)
            if found is not None:
                return found
        elif value is not None:
            tier_id = value
    rows = schedule.get('feeTiers', schedule.get('tiers', schedule.get('data', [])))
    if isinstance(rows, dict):
        rows = rows.get('feeTiers', rows.get('tiers', []))
    if not isinstance(rows, list):
        return None
    matches = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_id = row.get('id') or row.get('tierId') or row.get('level') or row.get('name')
        if str(row_id) == str(tier_id):
            value = rate(row)
            if value is not None:
                matches.append(value)
    if len(matches) == 1:
        return matches[0]

    # An account with no trading history may omit its tier entirely.  The
    # schedule's level-0 row has a zero volume threshold and is the only safe
    # conservative default; it avoids treating an unknown fee as zero while
    # still allowing a newly funded account to pass the read-only gate.
    if tier_id is None:
        base_rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                threshold = float(row.get('volume_threshold', row.get('volumeThreshold', 0)))
            except (TypeError, ValueError):
                continue
            value = rate(row)
            if value is not None:
                base_rows.append((threshold, value))
        if base_rows:
            minimum = min(item[0] for item in base_rows)
            candidates = [value for threshold, value in base_rows if threshold == minimum]
            if len(candidates) == 1:
                return candidates[0]
    return None
