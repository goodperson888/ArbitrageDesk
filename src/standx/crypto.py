"""Small dependency-free Ed25519 and base58 helpers for StandX signing.

The execution environment intentionally keeps signing code isolated from the
public monitor.  These routines implement RFC 8032's Ed25519 signing formula
so a fresh temporary key can be used for StandX request-body signatures.
"""
from __future__ import annotations

import hashlib
import os


_Q = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _Q - 2, _Q)) % _Q
_I = pow(2, (_Q - 1) // 4, _Q)
_B = (15112221349535400772501151409588531511454012693041857206046113283949847762202,
      46316835694926478169428394003475163141307993866256225615783033603165251855960)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * pow(_D * y * y + 1, _Q - 2, _Q) % _Q
    x = pow(xx, (_Q + 3) // 8, _Q)
    if (x * x - xx) % _Q:
        x = x * _I % _Q
    if x & 1:
        x = _Q - x
    return x


def _edwards_add(p, q):
    x1, y1 = p
    x2, y2 = q
    den_x = pow(1 + _D * x1 * x2 * y1 * y2, _Q - 2, _Q)
    den_y = pow(1 - _D * x1 * x2 * y1 * y2, _Q - 2, _Q)
    return ((x1 * y2 + x2 * y1) * den_x % _Q,
            (y1 * y2 + x1 * x2) * den_y % _Q)


def _scalarmult(point, scalar: int):
    result = (0, 1)
    addend = point
    while scalar:
        if scalar & 1:
            result = _edwards_add(result, addend)
        addend = _edwards_add(addend, addend)
        scalar >>= 1
    return result


def _encodepoint(point) -> bytes:
    x, y = point
    value = y | ((x & 1) << 255)
    return value.to_bytes(32, "little")


def public_key(seed: bytes) -> bytes:
    if len(seed) != 32:
        raise ValueError("Ed25519 seed must be 32 bytes")
    digest = bytearray(hashlib.sha512(seed).digest())
    digest[0] &= 248
    digest[31] &= 63
    digest[31] |= 64
    scalar = int.from_bytes(digest[:32], "little")
    return _encodepoint(_scalarmult(_B, scalar))


def sign(message: bytes, seed: bytes) -> bytes:
    if len(seed) != 32:
        raise ValueError("Ed25519 seed must be 32 bytes")
    digest = bytearray(hashlib.sha512(seed).digest())
    digest[0] &= 248
    digest[31] &= 63
    digest[31] |= 64
    scalar = int.from_bytes(digest[:32], "little")
    pub = _encodepoint(_scalarmult(_B, scalar))
    nonce = int.from_bytes(hashlib.sha512(bytes(digest[32:]) + message).digest(), "little") % _L
    encoded_r = _encodepoint(_scalarmult(_B, nonce))
    challenge = int.from_bytes(hashlib.sha512(encoded_r + pub + message).digest(), "little") % _L
    s = (nonce + challenge * scalar) % _L
    return encoded_r + s.to_bytes(32, "little")


def generate_seed() -> bytes:
    return os.urandom(32)


def base58_encode(data: bytes) -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    number = int.from_bytes(data, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = alphabet[remainder] + encoded
    prefix = len(data) - len(data.lstrip(b"\0"))
    return "1" * prefix + (encoded or "")

