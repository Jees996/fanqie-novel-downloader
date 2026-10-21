# -*- coding: utf-8 -*-
"""番茄 APP 正文接口（``reading.snssdk.com``）纯算法客户端。

为什么需要这一层
----------------
网页版 ``fanqienovel.com/reader/{item_id}`` 对未登录用户只放前 10 章，之后
一律返回「扫码下载 APP 免费读」的会员门页；而 APP 接口本身允许匿名设备
免费阅读全书（广告模式），所以直接走 APP 网关：

1. ``POST i.snssdk.com/service/2/device_register`` 注册一台匿名设备
   （请求体经 TTEncrypt：gzip + SHA512 + AES-CBC），拿到 ``device_id`` /
   ``install_id``；
2. ``POST /reading/crypt/registerkey`` 用静态密钥换正文解密密钥（AES-CBC）；
3. ``GET /reading/reader/batch_full/v`` 批量取章节（每批最多 30 章），
   正文是 AES-CBC 加密的 XHTML，解密后即为**明文段落**（无字体混淆）。

请求头需要 ``x-argus`` / ``x-ladon`` / ``x-khronos`` 三件套签名：
- ``x-argus``：protobuf 明文 → SM3 派生 Simon-4-72 密钥加密 → 异或/倒序 → AES-CBC；
- ``x-ladon``：SPECK-64 加密 ``{ts}-{license_id}-{aid}``。

设备与密钥缓存在项目根目录 ``.fanqie_device.json``，首次下载自动注册，
之后复用；设备被服务端作废（HTTP 200 空响应）时会自动换新设备。

限速
----
实测同一设备连续请求会被限流（后续批次只回 1 章正文甚至空响应），
因此默认每批间隔 ``interval`` 秒（6 秒）串行请求，30 章/批，全本约 1~2 分钟。

签名失效
--------
``x-argus`` / ``x-ladon`` / 设备注册字段依赖番茄 APP 当前版本的固定参数，
**番茄改版后可能失效**。若下载时报「APP 接口不可用 / 空响应 / 正文密钥失效」，
请到仓库提交 issue（附书籍 ID、完整报错与运行环境）：

    https://github.com/Jees996/fanqie-novel-downloader/issues
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import html as html_lib
import json
import os
import random
import re
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import requests

try:  # 作为包导入
    from .decryptor import remaining_pua
except ImportError:  # 兼容 `python core/appapi.py` 直接运行
    import pathlib as _pathlib
    import sys as _sys

    _root = str(_pathlib.Path(__file__).resolve().parent.parent)
    if _root not in _sys.path:
        _sys.path.insert(0, _root)
    from core.decryptor import remaining_pua

try:  # AES-CBC：优先 cryptography，缺失时给出明确提示
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:  # pragma: no cover
    Cipher = algorithms = modes = None  # type: ignore[assignment]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE_PATH = PROJECT_ROOT / ".fanqie_device.json"

# --------------------------------------------------------------------------- #
# 常量（来自 APP 7.0.1.32 的鉴权参数）
# --------------------------------------------------------------------------- #
_MASK64 = (1 << 64) - 1
_AID = "1967"
_LICENSE_ID = "1611921764"
_SDK_VERSION = "v04.04.05-ov-android"
_SDK_VERSION_INT = 134744640
_CALL_TYPE = 738
_SIGN_KEY = bytes.fromhex(
    "ac1adaae95a7af94a5114ab3b3a97dd80050aa0a39314c40528caec95256c28c")
# b64decode("rCXGfd2POMGzeiNIgo4iLg==")：正文 / 密钥注册的静态 AES 密钥
_SHARED_KEY = bytes([172, 37, 198, 125, 221, 143, 56, 193,
                     179, 122, 35, 72, 130, 142, 34, 46])
_FIXED_STRING = base64.b64decode(
    "TdTC5rgxYgkOUrPHpnM7pByyRiuCmrWKGWs521cXdST0m69/COjWjSanLjfBqVovHwWlGJKu8pSXMrYqOKrdWA==")

_LOW_RAND = bytes([0xF2, 0x81])
_HIGH_RAND = bytes([0x61, 0x6F])
_XOR_PREFIX = bytes([0xF2, 0xF7, 0xFC, 0xFF, 0xF2, 0xF7, 0xFC, 0xFF])
_ARGUS_HEADER = bytes([0xA6, 0x6E, 0xAD, 0x9F, 0x77, 0x01, 0xD0, 0x0C, 0x18])

_APP_BASE = "https://reading.snssdk.com/reading"
_APP_UA = "com.dragon.read"
_REGISTER_URL = "https://i.snssdk.com/service/2/device_register/?tt_data=a"

_ANDROID_VERSIONS = [("9", 29), ("10", 30), ("11", 31), ("12", 32), ("13", 33), ("14", 34)]
_DEVICE_MODELS = [
    "RMX1931", "MI8", "Honor10", "P30", "V1921A", "Redmi Note 7", "Redmi K20 Pro",
    "MI 9", "Mi 10 Pro", "SM-G9750", "Pixel 6", "HD1910", "M2011K2C", "LIO-AN00",
    "VOG-TL00", "PCLM10", "GM1900", "Pixel 7 Pro", "Pixel 6a", "SM-N9760", "POCO F1",
]
_DEVICE_BRANDS = [
    "realme", "Xiaomi", "Huawei", "OPPO", "vivo", "samsung", "OnePlus",
    "google", "Redmi", "HONOR", "motorola", "POCO",
]
_HEX_LOW = "0123456789abcdef"

_CHAPTER_TAG_RE = re.compile(r"<[^>]+>")
_P_RE = re.compile(r"<p(?:\s[^>]*)?>(.*?)</p>", re.IGNORECASE | re.DOTALL)
_BLK_RE = re.compile(r"<blk(?:\s[^>]*)?>(.*?)</blk>", re.IGNORECASE | re.DOTALL)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_STYLE_RE = re.compile(r"<style[^>]*>.*?</style>", re.IGNORECASE | re.DOTALL)
_WS_RE = re.compile(r"[ \t\r\f\v\u00a0]+")


class AppApiError(Exception):
    """APP 接口不可用（设备注册失败 / 网关拒绝 / 空响应等）。"""


# --------------------------------------------------------------------------- #
# 基础密码学
# --------------------------------------------------------------------------- #
def _aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    pad = 16 - len(data) % 16
    data = data + bytes([pad]) * pad
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return enc.update(data) + enc.finalize()


def _aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    out = dec.update(data) + dec.finalize()
    return out[: -out[-1]] if out else out


def _md5(data: bytes) -> bytes:
    return hashlib.md5(data).digest()


def _md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _sha512(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


def _rotl32(x: int, n: int) -> int:
    n &= 31
    return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF


def _sm3_p0(x: int) -> int:
    return (x ^ _rotl32(x, 9) ^ _rotl32(x, 17)) & 0xFFFFFFFF


def _sm3_p1(x: int) -> int:
    return (x ^ _rotl32(x, 15) ^ _rotl32(x, 23)) & 0xFFFFFFFF


def sm3(data: bytes) -> bytes:
    """SM3 摘要（GB/T 32905-2016）。"""
    bit_len = len(data) * 8
    padded = bytearray(data) + b"\x80"
    while len(padded) % 64 != 56:
        padded += b"\x00"
    padded += bit_len.to_bytes(8, "big")

    state = [0x7380166F, 0x4914B2B9, 0x172442D7, 0xDA8A0600,
             0xA96F30BC, 0x163138AA, 0xE38DEE4D, 0xB0FB0E4E]
    for off in range(0, len(padded), 64):
        block = padded[off:off + 64]
        w = [int.from_bytes(block[i * 4:i * 4 + 4], "big") for i in range(16)]
        for i in range(16, 68):
            w.append(_sm3_p1(w[i - 16] ^ w[i - 9] ^ _rotl32(w[i - 3], 15))
                     ^ _rotl32(w[i - 13], 7) ^ w[i - 6])
        w1 = [w[i] ^ w[i + 4] for i in range(64)]

        a, b, c, d, e, f, g, h = state
        for i in range(64):
            t = 0x79CC4519 if i <= 15 else 0x7A879D8A
            ss1 = _rotl32((_rotl32(a, 12) + e + _rotl32(t, i)) & 0xFFFFFFFF, 7)
            ss2 = ss1 ^ _rotl32(a, 12)
            if i <= 15:
                ff, gg = a ^ b ^ c, e ^ f ^ g
            else:
                ff, gg = (a & b) | (a & c) | (b & c), (e & f) | (~e & g)
            tt1 = (ff + d + ss2 + w1[i]) & 0xFFFFFFFF
            tt2 = (gg + h + ss1 + w[i]) & 0xFFFFFFFF
            d, c, b, a = c, _rotl32(b, 9), a, tt1
            h, g, f, e = g, _rotl32(f, 19), e, _sm3_p0(tt2)
        state = [x ^ y for x, y in zip(state, (a, b, c, d, e, f, g, h))]
    return b"".join(x.to_bytes(4, "big") for x in state)


def _rol64(v: int, n: int) -> int:
    return ((v << n) | (v >> (64 - n))) & _MASK64


def _ror64(v: int, n: int) -> int:
    return ((v << (64 - n)) | (v >> n)) & _MASK64


def _simon_key_expand(key: Tuple[int, int, int, int]) -> List[int]:
    z4 = 0x3DC94C3A046D678B
    k = list(key)
    for i in range(4, 72):
        tmp = _ror64(k[i - 1], 3)
        tmp ^= k[i - 3]
        tmp ^= _ror64(tmp, 1)
        bit = (z4 >> ((i - 4) % 62)) & 1
        k.append((~k[i - 4] & _MASK64) ^ tmp ^ bit ^ 3)
    return k


def _simon_encrypt(pt: Tuple[int, int], key: Tuple[int, int, int, int]) -> Tuple[int, int]:
    k = _simon_key_expand(key)
    x, y = pt[0] & _MASK64, pt[1] & _MASK64
    for i in range(72):
        tmp = y
        f = _rol64(y, 1) & _rol64(y, 8)
        y = (x ^ f ^ _rol64(y, 2) ^ k[i]) & _MASK64
        x = tmp
    return x, y


def _speck_key_schedule(key: bytes) -> List[int]:
    ks = [int.from_bytes(key[0:8], "little")]
    ls = [int.from_bytes(key[i * 8:(i + 1) * 8], "little")
          for i in range(1, len(key) // 8)]
    for x in range(34 - 1):
        rs_x = ((ls[x] << 56) | (ls[x] >> 8)) & _MASK64
        new_x = (x ^ ((rs_x + ks[x]) & _MASK64)) & _MASK64
        ls_y = ((ks[x] >> 61) | (ks[x] << 3)) & _MASK64
        ls.append(new_x)
        ks.append((new_x ^ ls_y) & _MASK64)
    return ks


def _speck_encrypt(key: bytes, plaintext: bytes) -> bytes:
    pad = 16 - len(plaintext) % 16
    padded = plaintext + bytes([pad]) * pad
    ks = _speck_key_schedule(key)
    out = bytearray()
    for i in range(0, len(padded), 16):
        y = int.from_bytes(padded[i:i + 8], "little")
        x = int.from_bytes(padded[i + 8:i + 16], "little")
        for k in ks:
            x = (k ^ ((((x << 56) | (x >> 8)) & _MASK64) + y) & _MASK64) & _MASK64
            y = (x ^ (((y >> 61) | (y << 3)) & _MASK64)) & _MASK64
        out += y.to_bytes(8, "little") + x.to_bytes(8, "little")
    return bytes(out)


class _ProtobufWriter:
    """只实现 X-Argus 需要的 wire 类型（varint / length-delimited）。"""

    def __init__(self) -> None:
        self.buf = bytearray()

    def _varint(self, value: int) -> "_ProtobufWriter":
        value &= 0xFFFFFFFFFFFFFFFF
        while value >= 0x80:
            self.buf.append((value & 0x7F) | 0x80)
            value >>= 7
        self.buf.append(value & 0x7F)
        return self

    def _key(self, field: int, wire: int) -> "_ProtobufWriter":
        return self._varint((field << 3) | wire)

    def varint(self, field: int, value: int) -> "_ProtobufWriter":
        return self._key(field, 0)._varint(value)

    def bytes_(self, field: int, data: bytes) -> "_ProtobufWriter":
        self._key(field, 2)._varint(len(data))
        self.buf += data
        return self

    def string(self, field: int, value: str) -> "_ProtobufWriter":
        return self.bytes_(field, value.encode("utf-8"))

    def message(self, field: int, build: Callable[["_ProtobufWriter"], None]) -> "_ProtobufWriter":
        sub = _ProtobufWriter()
        build(sub)
        return self.bytes_(field, bytes(sub.buf))

    def to_bytes(self) -> bytes:
        return bytes(self.buf)


def _sm3_prefix6(data: bytes) -> bytes:
    return sm3(data)[:6]


def _get_argus(query: str, xss_stub: str, timestamp: int) -> str:
    params = dict(part.split("=", 1) for part in query.split("&") if "=" in part)
    device_id = params.get("device_id", "")
    version_name = params.get("version_name", "")

    body_hash = _sm3_prefix6(bytes(16) if not xss_stub else bytes.fromhex(xss_stub[:32]))
    query_hash = _sm3_prefix6(query.encode("utf-8") if query else bytes(16))

    pb = _ProtobufWriter()
    pb.varint(1, 0x20200929 * 2)
    pb.varint(2, 2)
    pb.varint(3, random.getrandbits(31))
    pb.string(4, _AID)
    pb.string(5, device_id)
    pb.string(6, _LICENSE_ID)
    pb.string(7, version_name)
    pb.string(8, _SDK_VERSION)
    pb.varint(9, _SDK_VERSION_INT)
    pb.bytes_(10, bytes(8))
    pb.varint(11, 0)
    pb.varint(12, timestamp * 2)
    pb.bytes_(13, body_hash)
    pb.bytes_(14, query_hash)
    pb.message(15, lambda sub: (sub.varint(1, 1), sub.varint(2, 1),
                                sub.varint(3, 1), sub.varint(7, 3348294860)))
    pb.string(16, "")
    pb.string(20, "none")
    pb.varint(21, _CALL_TYPE)
    pb.message(23, lambda sub: (sub.string(1, "NX551J"), sub.varint(2, 8196),
                                sub.varint(4, 2162219008)))
    pb.varint(25, 2)
    protobuf = pb.to_bytes()
    pad = 16 - len(protobuf) % 16
    protobuf += bytes([pad]) * pad

    digest = sm3(_SIGN_KEY + _LOW_RAND + _HIGH_RAND + _SIGN_KEY)
    key = tuple(int.from_bytes(digest[i * 8:(i + 1) * 8], "little") for i in range(4))

    encrypted = bytearray()
    for off in range(0, len(protobuf), 16):
        low = int.from_bytes(protobuf[off:off + 8], "little")
        high = int.from_bytes(protobuf[off + 8:off + 16], "little")
        e_low, e_high = _simon_encrypt((low, high), key)
        encrypted += e_low.to_bytes(8, "little") + e_high.to_bytes(8, "little")

    data = bytearray(_XOR_PREFIX + bytes(encrypted))
    for i in range(8, len(data)):
        data[i] ^= data[i % 8]
    data.reverse()

    plaintext = _ARGUS_HEADER + bytes(data) + _HIGH_RAND
    ciphertext = _aes_cbc_encrypt(_md5(_SIGN_KEY[:16]), _md5(_SIGN_KEY[16:]), plaintext)
    return base64.b64encode(_LOW_RAND + ciphertext).decode()


def _get_ladon(timestamp: int) -> str:
    random_bytes = os.urandom(4)
    plaintext = f"{timestamp}-{_LICENSE_ID}-{_AID}".encode()
    key = _md5_hex(random_bytes + _AID.encode()).encode()
    return base64.b64encode(random_bytes + _speck_encrypt(key, plaintext)).decode()


def sign_request(url: str, body: Optional[bytes] = None) -> Dict[str, str]:
    """生成 APP 网关要求的签名头。"""
    from urllib.parse import urlsplit

    raw_query = urlsplit(url).query
    xss_stub = _md5_hex(body) if body else ""
    now_ms = int(time.time() * 1000)
    ts = now_ms // 1000
    headers = {
        "x-argus": _get_argus(raw_query, xss_stub, ts),
        "x-ladon": _get_ladon(ts),
        "x-khronos": str(ts),
        "x-ss-req-ticket": str(now_ms),
    }
    if body:
        headers["X-SS-STUB"] = xss_stub
    return headers


def _tt_encrypt(data: bytes) -> bytes:
    """设备注册请求体加密（TTEncrypt：gzip + SHA512 + AES-CBC）。"""
    random_bytes = os.urandom(32)
    hash_value = _sha512(_sha512(random_bytes) + _FIXED_STRING)
    key, iv = hash_value[:16], hash_value[16:32]
    compressed = gzip.compress(data, mtime=0)
    encrypted = _aes_cbc_encrypt(key, iv, _sha512(compressed) + compressed)
    return bytes([116, 99, 5, 16, 0, 0]) + random_bytes + encrypted


# --------------------------------------------------------------------------- #
# 设备 / 密钥注册
# --------------------------------------------------------------------------- #
def _rand_hex(k: int) -> str:
    return "".join(_HEX_LOW[b % 16] for b in os.urandom(k))


def _uuid() -> str:
    b = bytearray(os.urandom(16))
    b[6] = (b[6] & 0x0F) | 0x40
    b[8] = (b[8] & 0x3F) | 0x80
    h = b.hex()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def _ipv6_unique_local() -> str:
    s = f"fd{_HEX_LOW[random.randrange(256) >> 4]}{_HEX_LOW[random.randrange(256) & 0x0F]}"
    return s + "".join(":" + f"{random.randrange(1 << 16):04x}" for _ in range(7))


def _build_register_body() -> dict:
    os_version, os_api = random.choice(_ANDROID_VERSIONS)
    brand = random.choice(_DEVICE_BRANDS)
    gen_time = int(time.time() * 1000)
    rom_version = ("coloros__" + _rand_hex(4).upper()
                   + "." + str(random.randrange(1_000_000)).zfill(6)
                   + "." + str(random.randrange(100_000_000)).zfill(8) + " release-keys")
    return {
        "magic_tag": "ss_app_log",
        "header": {
            "display_name": "番茄免费小说", "aid": 1967, "channel": "43536163a",
            "package": "com.dragon.read", "sdk_version": "3.7.0-rc.25-fanqie-xiaoshuo",
            "sdk_target_version": 29, "git_hash": "711d1a7", "density_dpi": 240,
            "display_density": "hdpi", "resolution": "720x1280", "language": "zh",
            "timezone": 8, "access": "wifi", "not_request_sender": 0,
            "carrier": "CHINA MOBILE", "mcc_mnc": "46000", "region": "CN",
            "tz_name": "Asia/Shanghai", "tz_offset": 28800, "sim_region": "cn",
            "sim_serial_number": [], "oaid_may_support": False,
            "device_platform": "android",
            "custom": {"host_bit": 32, "dragon_device_type": 0},
            "pre_installed_channel": "", "is_system_app": 0, "sdk_flavor": "china",
            "guest_mode": 0, "os": "Android", "os_version": os_version, "os_api": os_api,
            "device_model": random.choice(_DEVICE_MODELS), "device_brand": brand,
            "device_manufacturer": brand, "cpu_abi": "arm64-v8a",
            "release_build": _rand_hex(7), "cdid": _uuid(),
            "sig_hash": "a4a27c2633195374c15651ffc3c4a497", "openudid": _rand_hex(20),
            "clientudid": _uuid(), "req_id": _uuid(),
            "rom": _rand_hex(14).upper(), "rom_version": rom_version,
            "apk_first_install_time": gen_time - random.randrange(365) * 86_400_000,
            "ipv6_list": [
                {"type": "client_anpi", "value": "::1"},
                {"type": "client_anpi", "value": _ipv6_unique_local()},
                {"type": "client_anpi", "value": _ipv6_unique_local()},
            ],
        },
        "_gen_time": gen_time,
    }


def _build_query(device: dict, extra: Optional[dict] = None) -> str:
    from urllib.parse import urlencode

    params = {
        "iid": device["install_id"],
        "device_id": device["device_id"],
        "ac": "wifi", "channel": "43536163a", "aid": _AID, "app_name": "novelapp",
        "version_code": "70132", "version_name": "7.0.1.32",
        "device_platform": "android", "os": "android", "ssmix": "a", "os_version": "10",
        "device_type": device.get("device_type") or "P30",
        "device_brand": device.get("device_brand") or "realme",
        "update_version_code": "70132", "manifest_version_code": "70132",
        "pv_player": "70132",
    }
    if extra:
        params.update(extra)
    return urlencode(params)


def _reverse_hex(device_id: str) -> str:
    be = format(int(device_id), "x").zfill(32)
    return "".join(be[i - 2:i] for i in range(len(be), 0, -2))


# --------------------------------------------------------------------------- #
# 正文解析
# --------------------------------------------------------------------------- #
def _clean_fragment(fragment: str) -> str:
    text = _CHAPTER_TAG_RE.sub("", fragment)
    text = html_lib.unescape(text)
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = text.replace("\u3000", " ")
    text = _WS_RE.sub(" ", text)
    return text.strip()


def parse_chapter_xhtml(xhtml: str) -> Tuple[str, List[str]]:
    """解析 APP 下发的章节 XHTML，返回 ``(章节标题, 段落列表)``。

    正文结构形如::

        <article><h1 class="chapterTitle1"><blk>第101章 标题</blk></h1>
        <p idx="0" p_idx="40000"><blk>段落文本</blk></p>…</article>

    正文为明文（无字体混淆），只需去标签 / 反转义。
    """
    if not xhtml:
        return "", []
    xhtml = _STYLE_RE.sub("", xhtml)

    title = ""
    match = _H1_RE.search(xhtml)
    if match:
        title = _clean_fragment(match.group(1))

    paragraphs: List[str] = []
    for raw in _P_RE.findall(xhtml):
        text = _clean_fragment(raw)
        if text:
            paragraphs.append(text)
    if not paragraphs:  # 兜底：没有 <p> 时按 <blk> 收集
        for raw in _BLK_RE.findall(xhtml):
            text = _clean_fragment(raw)
            if text and text != title:
                paragraphs.append(text)
    return title, paragraphs


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
class FanqieAppApi:
    """匿名设备 + 签名的 APP 正文客户端。

    Args:
        timeout: 单请求超时秒数。
        interval: 相邻两批请求的最小间隔（秒），过低会被限流。
        batch_size: 每批章节数，服务端上限 30。
        cache_path: 设备/密钥缓存文件；``None`` 表示只保存在内存。
        max_devices: 单次下载最多注册多少台设备（设备被作废时换新）。
    """

    def __init__(self, timeout: int = 30, interval: float = 6.0,
                 batch_size: int = 30,
                 cache_path: Optional[str | os.PathLike] = DEFAULT_CACHE_PATH,
                 max_devices: int = 3):
        if Cipher is None:  # pragma: no cover
            raise AppApiError("缺少 cryptography 依赖，无法使用 APP 接口")
        self.timeout = max(5, int(timeout))
        self.interval = max(0.0, float(interval))
        self.batch_size = max(1, min(30, int(batch_size)))
        self.max_devices = max(1, int(max_devices))
        self.cache_path = Path(cache_path) if cache_path else None
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": _APP_UA,
            "Accept": "*/*",
        })
        self.device: Optional[dict] = None
        self.key: Optional[bytes] = None
        self.key_version: Optional[int] = None
        self.devices_made = 0
        self._last_request = 0.0

    # ------------------------------------------------------------- 缓存
    def _load_cache(self) -> bool:
        if not self.cache_path or not self.cache_path.is_file():
            return False
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            device = data.get("device") or {}
            if not device.get("device_id") or not device.get("install_id"):
                return False
            self.device = device
            if data.get("key"):
                self.key = bytes.fromhex(data["key"])
                self.key_version = data.get("key_version")
            return True
        except (OSError, ValueError, TypeError):
            return False

    def _save_cache(self) -> None:
        if not self.cache_path:
            return
        payload = {
            "device": self.device,
            "key": self.key.hex() if self.key else "",
            "key_version": self.key_version,
            "updated_at": int(time.time()),
        }
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, self.cache_path)
        except OSError:
            pass

    # ------------------------------------------------------------- 注册
    def _register_device(self) -> dict:
        body = _build_register_body()
        plain = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        try:
            response = self.session.post(
                _REGISTER_URL, data=_tt_encrypt(plain),
                headers={"User-Agent": "okhttp/4.10.0",
                         "Content-Type": "application/octet-stream; tt-data=a"},
                timeout=self.timeout)
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise AppApiError(f"匿名设备注册失败：{exc}") from None
        if not payload.get("device_id_str") or not payload.get("install_id_str"):
            raise AppApiError("匿名设备注册失败：响应缺少 device_id / install_id")
        device = {
            "device_id": str(payload["device_id_str"]),
            "install_id": str(payload["install_id_str"]),
            "device_type": body["header"]["device_model"],
            "device_brand": body["header"]["device_brand"],
        }
        self.devices_made += 1
        return device

    def _register_key(self) -> None:
        if not self.device:
            raise AppApiError("尚未注册设备")
        url = f"{_APP_BASE}/crypt/registerkey?{_build_query(self.device)}"
        iv = "".join(random.choice(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")
            for _ in range(16)).encode()
        data = bytes.fromhex(_reverse_hex(self.device["device_id"]))[:8]
        body = json.dumps({
            "content": base64.b64encode(
                iv + _aes_cbc_encrypt(_SHARED_KEY, iv, data)).decode()
        }, separators=(",", ":")).encode()
        headers = sign_request(url, body)
        headers.update({"Content-Type": "application/json; charset=utf-8"})
        try:
            response = self.session.post(url, data=body, headers=headers,
                                         timeout=self.timeout)
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise AppApiError(f"正文密钥注册失败：{exc}") from None
        data_obj = payload.get("data") or {}
        if not data_obj.get("key"):
            raise AppApiError("正文密钥注册失败：响应缺少 key")
        raw = base64.b64decode(data_obj["key"])
        self.key = _aes_cbc_decrypt(_SHARED_KEY, raw[:16], raw[16:])
        self.key_version = data_obj.get("keyver")

    def ensure_ready(self) -> None:
        """确保设备与正文密钥就绪（优先读缓存）。"""
        if self.device and self.key:
            return
        if not self.device:
            if not self._load_cache():
                self.device = self._register_device()
        if not self.key:
            self._register_key()
        self._save_cache()

    def _recover_device(self) -> None:
        """设备被作废（空响应）时换一台新设备。"""
        if self.devices_made >= self.max_devices:
            raise AppApiError("APP 接口持续返回空响应，设备注册次数已达上限，请稍后重试")
        self.device = self._register_device()
        self.key = None
        self._register_key()
        self._save_cache()

    # ------------------------------------------------------------- 请求
    def _throttle(self) -> None:
        wait = self.interval - (time.time() - self._last_request)
        if wait > 0:
            time.sleep(wait)

    def _fetch_batch(self, item_ids: List[str], book_id: str) -> Dict[str, str]:
        self.ensure_ready()
        self._throttle()
        url = f"{_APP_BASE}/reader/batch_full/v?" + _build_query(self.device, {
            "item_ids": ",".join(item_ids),
            "book_id": book_id or "0",
            "novel_text_type": "1",
            "req_type": "1",
        })
        headers = sign_request(url)
        try:
            response = self.session.get(url, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise AppApiError(f"章节接口请求失败：{exc}") from None
        self._last_request = time.time()
        if response.status_code != 200:
            raise AppApiError(f"章节接口返回 HTTP {response.status_code}")
        if not response.content:
            raise AppApiError("章节接口返回空响应（设备可能被作废）")
        try:
            payload = response.json()
        except ValueError:
            raise AppApiError("章节接口返回了非 JSON 数据") from None
        data = payload.get("data")
        if not isinstance(data, dict) or not data:
            raise AppApiError("章节接口返回空响应（设备可能被作废）")

        result: Dict[str, str] = {}
        invalid = 0
        for item_id, item in data.items():
            content = item.get("content")
            if not content or content == "Invalid":
                if content == "Invalid":
                    invalid += 1
                continue
            raw = base64.b64decode(content)
            plain = _aes_cbc_decrypt(self.key, raw[:16], raw[16:])
            if item.get("compress_status") == 1:
                plain = gzip.decompress(plain)
            result[str(item_id)] = plain.decode("utf-8", "replace")
        if not result and invalid:
            # 正文密钥过期时服务端回 "Invalid"，重新注册密钥即可
            raise AppApiError("正文密钥失效，需要重新注册")
        return result

    # ------------------------------------------------------------- 对外接口
    def fetch_chapters(self, item_ids: Iterable[str], book_id: str = "",
                       on_batch: Optional[Callable[[List[str], Dict[str, str]], None]] = None,
                       max_rounds: int = 3) -> Dict[str, str]:
        """批量取章节正文，返回 ``{item_id: xhtml}``。

        自动处理限流（间隔）、部分返回（缺失章节下一轮重试）、
        设备作废（换新设备）、密钥失效（重新注册）。
        """
        pending = [str(i) for i in item_ids if i]
        result: Dict[str, str] = {}
        if not pending:
            return result

        for rnd in range(max(1, max_rounds)):
            if not pending:
                break
            if rnd > 0:
                time.sleep(max(self.interval, 8.0))
            still_missing: List[str] = []
            for start in range(0, len(pending), self.batch_size):
                batch = pending[start:start + self.batch_size]
                if start > 0:
                    self._throttle()
                try:
                    got = self._fetch_batch(batch, book_id)
                except AppApiError as exc:
                    message = str(exc)
                    if "空响应" in message:
                        # 设备被限流/作废：冷却后换设备重试
                        time.sleep(15)
                        self._recover_device()
                        try:
                            got = self._fetch_batch(batch, book_id)
                        except AppApiError as retry_exc:
                            raise AppApiError(str(retry_exc)) from None
                    elif "密钥失效" in message:
                        # 正文密钥过期：重新注册密钥后重试同一批
                        self.key = None
                        self._register_key()
                        try:
                            got = self._fetch_batch(batch, book_id)
                        except AppApiError as retry_exc:
                            raise AppApiError(str(retry_exc)) from None
                    else:
                        raise
                result.update(got)
                still_missing.extend(item for item in batch if item not in got)
                if on_batch:
                    try:
                        on_batch(batch, got)
                    except Exception:
                        pass
            pending = still_missing
        return result

    def fetch_chapter(self, item_id: str, book_id: str = "") -> Tuple[str, List[str]]:
        """取单章并解析，返回 ``(标题, 段落列表)``。"""
        result = self.fetch_chapters([item_id], book_id)
        xhtml = result.get(str(item_id))
        if not xhtml:
            raise AppApiError("该章节未能获取正文")
        return parse_chapter_xhtml(xhtml)

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 自测：python core/appapi.py [book_id]
# --------------------------------------------------------------------------- #
def _self_test() -> int:
    import sys as _sys

    book_id = _sys.argv[1] if len(_sys.argv) > 1 else "7527891257914297368"
    api = FanqieAppApi(interval=6.0)
    try:
        api.ensure_ready()
        print(f"[OK] 设备 {api.device['device_id']}，密钥版本 {api.key_version}")
    except AppApiError as exc:
        print(f"[FAIL] 设备/密钥注册失败：{exc}")
        return 1

    try:
        from .crawler import FanqieCrawler
    except ImportError:
        from core.crawler import FanqieCrawler

    chapters = FanqieCrawler().fetch_chapter_list(book_id)
    if not chapters:
        print("[FAIL] 目录为空")
        return 1
    ids = [c["item_id"] for c in chapters[:5] if c["item_id"]]
    print(f"书籍 {book_id}：共 {len(chapters)} 章，测试前 {len(ids)} 章")
    try:
        result = api.fetch_chapters(ids, book_id)
    except AppApiError as exc:
        print(f"[FAIL] 正文获取失败：{exc}")
        return 1

    pua_total = 0
    for item_id in ids:
        xhtml = result.get(item_id)
        if not xhtml:
            print(f"[FAIL] 章节 {item_id} 未返回正文")
            return 1
        title, paragraphs = parse_chapter_xhtml(xhtml)
        text = "\n".join(paragraphs)
        leftover = remaining_pua(text)
        pua_total += len(leftover)
        print(f"  {title} | {len(paragraphs)} 段 | {text[:40]}…")
    if pua_total:
        print(f"[FAIL] 仍有 {pua_total} 个 PUA 字符")
        return 1
    print(f"[OK] APP 接口正文获取与解密正常，共 {len(result)} 章，无 PUA 残留")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
