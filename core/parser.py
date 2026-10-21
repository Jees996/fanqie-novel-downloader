# -*- coding: utf-8 -*-
"""番茄小说链接解析器。

支持三种输入形态：

1. App 分享出来的杂质文本，例如
   ``推荐一部好书《无人清白》https://changdunovel.com/t/ZPJkJpLOb-M/``
2. ``changdunovel.com`` 短链（需要跟随 302 拿到 book_id）
3. PC 网页链接 ``https://fanqienovel.com/page/7231424472606051384``
   或阅读器链接 ``https://fanqienovel.com/reader/{item_id}``

对外核心函数：

* :func:`extract_first_url` —— 从任意文本里抠出第一个 http(s) 链接
* :func:`resolve_book_id` —— 得到最终 15~20 位 book_id
* :func:`parse` —— resolve_book_id 的别名式封装，返回 ``{"book_id": ...}``
"""

from __future__ import annotations

import json
import re
import sys

import requests

# 链接匹配：排除中文标点与引号，避免把分享文案里的「，」「】」吃进来
URL_RE = re.compile(r"https?://[^\s<>\"'）)】\]，,。；;、]+", re.IGNORECASE)

# URL 参数里的 book_id / bookId
URL_BOOK_ID_RE = re.compile(r"(?:book_id|bookId|bookid|series_id)=(\d{10,25})",
                            re.IGNORECASE)
# 网页链接里的 book_id（page / reader 路径）
PATH_BOOK_ID_RE = re.compile(r"fanqienovel\.com/(?:page|reader)/(\d{10,25})",
                             re.IGNORECASE)
# 用户直接粘贴纯数字
BARE_ID_RE = re.compile(r"^\s*(\d{15,25})\s*$")

# 页面 HTML 中的兜底线索（分享页 / __INITIAL_STATE__ / 内联 JSON 等）
HTML_BOOK_ID_RES = [
    re.compile(r'"book_id"\s*:\s*"?(\d{10,25})'),
    re.compile(r'"bookId"\s*:\s*"?(\d{10,25})'),
    re.compile(r'book_id=(\d{10,25})'),
    re.compile(r'/page/(\d{15,25})'),
]

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
    "Mobile/15E148 Safari/604.1"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class ParseError(Exception):
    """链接无法解析为 book_id 时抛出，message 为可直接展示给用户的中文提示。"""


def extract_first_url(text: str) -> str | None:
    """从任意分享文案中提取第一个 http(s) 链接。"""
    if not text:
        return None
    match = URL_RE.search(text)
    return match.group(0).strip() if match else None


def extract_book_id_from_text(text: str) -> str | None:
    """不做网络请求，直接从文本 / URL 字面量里提取 book_id。"""
    if not text:
        return None
    bare = BARE_ID_RE.match(text)
    if bare:
        return bare.group(1)
    for regex in (PATH_BOOK_ID_RE, URL_BOOK_ID_RE):
        match = regex.search(text)
        if match:
            return match.group(1)
    return None


def _book_id_from_html(content: str) -> str | None:
    """从网页 HTML 源码中兜底查找 book_id。"""
    if not content:
        return None
    for regex in HTML_BOOK_ID_RES:
        match = regex.search(content)
        if match:
            return match.group(1)
    return None


def resolve_book_id(text: str, timeout: int = 15, max_redirects: int = 8) -> str:
    """把用户输入（分享文案 / 短链 / PC 链接 / 纯数字）解析成 book_id。

    Raises:
        ParseError: 输入为空、没有链接、网络失败或跳转后仍找不到 book_id。
    """
    if text is None or not str(text).strip():
        raise ParseError("请输入番茄小说链接或分享文案")

    text = str(text).strip()

    # 1) 纯数字 book_id
    bare = BARE_ID_RE.match(text)
    if bare:
        return bare.group(1)

    # 2) 链接字面量里已经带 book_id 的情况，零网络请求直接返回
    direct = extract_book_id_from_text(text)
    if direct:
        return direct

    url = extract_first_url(text)
    if not url:
        raise ParseError("没有找到有效链接，请粘贴包含 http(s) 链接的分享文案")

    # 3) 跟随 302 重定向（短链 -> changdunovel 分享页）
    session = requests.Session()
    session.headers.update({
        "User-Agent": MOBILE_UA,
        "Referer": "https://changdunovel.com/",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    try:
        response = session.get(url, timeout=timeout, allow_redirects=True,
                               headers={"User-Agent": MOBILE_UA})
    except requests.exceptions.Timeout:
        raise ParseError("链接访问超时，请检查网络后重试") from None
    except requests.exceptions.RequestException as exc:
        raise ParseError(f"链接访问失败：{exc.__class__.__name__}") from None

    # 4) 依次检查：最终 URL、跳转链、响应 HTML
    candidates: list[str] = [response.url]
    candidates.extend(item.headers.get("location", "") for item in response.history)
    for candidate in candidates:
        if not candidate:
            continue
        found = extract_book_id_from_text(candidate)
        if found:
            return found

    found = _book_id_from_html(response.text)
    if found:
        return found

    # 5) 如果落地页是 reader 链接，再抓一次页面找 book_id
    if response.url and "/reader/" in response.url:
        try:
            page = session.get(response.url, timeout=timeout,
                               headers={"User-Agent": DESKTOP_UA})
            found = _book_id_from_html(page.text)
            if found:
                return found
        except requests.exceptions.RequestException:
            pass

    raise ParseError("未能从链接中提取 book_id，请换一条链接或直接粘贴 PC 端网页地址")


def parse(text: str) -> dict:
    """解析入口，返回 ``{"book_id": "..."}``。"""
    return {"book_id": resolve_book_id(text)}


if __name__ == "__main__":
    sample = sys.argv[1] if len(sys.argv) > 1 else (
        "推荐一部好书《无人清白》https://changdunovel.com/t/ZPJkJpLOb-M/"
    )
    try:
        result = parse(sample)
    except ParseError as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
    print(json.dumps({"success": True, "book_id": result["book_id"]},
                     ensure_ascii=False))
