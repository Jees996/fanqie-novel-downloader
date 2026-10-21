# -*- coding: utf-8 -*-
"""fanqie-novel-downloader 核心包。

模块划分：

* :mod:`core.parser`    —— 链接 / 分享文案解析，提取 book_id
* :mod:`core.decryptor` —— awesome-font 私有区字体解密（网页通道用）
* :mod:`core.appapi`    —— APP 正文接口（匿名设备 + 签名 + AES 解密，可下全书）
* :mod:`core.crawler`   —— 元数据、章节目录、正文抓取与 TXT 导出
"""

from .appapi import AppApiError, FanqieAppApi, parse_chapter_xhtml  # noqa: F401
from .crawler import (  # noqa: F401
    ChapterUnavailable,
    CrawlerError,
    FanqieCrawler,
    download_book,
    download_by_input,
)
from .decryptor import decrypt_text, has_obfuscation  # noqa: F401
from .parser import ParseError, extract_first_url, parse, resolve_book_id  # noqa: F401

__all__ = [
    "ParseError",
    "CrawlerError",
    "ChapterUnavailable",
    "AppApiError",
    "FanqieAppApi",
    "parse_chapter_xhtml",
    "FanqieCrawler",
    "download_book",
    "download_by_input",
    "decrypt_text",
    "has_obfuscation",
    "parse",
    "resolve_book_id",
    "extract_first_url",
]

__version__ = "1.1.0"
