# -*- coding: utf-8 -*-
"""章节抓取调度器。

数据通道
--------
1. **APP 正文通道**（主力，可下全书）
   ``reading.snssdk.com/reading/reader/batch_full/v``
   注册匿名设备 + 请求签名 + AES 解密后直接拿到**明文 XHTML 正文**，
   App 独占书、会员/SVIP 章节都能取到全文，无需登录。详见 :mod:`core.appapi`。

2. **落地页极速通道**（免登录）
   ``GET https://api.fqnovel.com/novel_ug/share/landing_page``
   返回书名 / 作者 / 评分 / 封面 / 简介 / 章节数，以及**前 10 章明文正文**。
   这部分完全不含字体混淆，是 APP 通道不可用时的兜底。

3. **PC 目录通道**
   ``GET https://fanqienovel.com/api/reader/directory/detail?bookId=...``
   返回全书 itemId 与章节标题（App 独占书也能拿到目录）。

4. **PC 阅读器通道**（兜底）
   ``GET https://fanqienovel.com/reader/{item_id}``
   服务端直出正文 HTML，段落位于 ``.muye-reader-content p``，但正文被
   ``awesome-font`` 私有区编码混淆，需交给 :mod:`core.decryptor` 解密。

APP 通道不可用时（设备注册失败 / 网络受限）自动回退网页通道；
网页通道对未登录用户只放前 10 章，此时 ``download_book`` 会明确返回
失败章节与原因，不会静默丢章。
"""

from __future__ import annotations

import html as html_lib
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

import requests

try:  # 作为包导入（app.py / python -m core.crawler）
    from .appapi import AppApiError, FanqieAppApi, parse_chapter_xhtml
    from .decryptor import decrypt_text, remaining_pua
    from .parser import ParseError, resolve_book_id
except ImportError:  # 兼容 `python core/crawler.py` 直接运行自测
    import pathlib as _pathlib

    _sys_path = str(_pathlib.Path(__file__).resolve().parent.parent)
    if _sys_path not in sys.path:
        sys.path.insert(0, _sys_path)
    from core.appapi import AppApiError, FanqieAppApi, parse_chapter_xhtml
    from core.decryptor import decrypt_text, remaining_pua
    from core.parser import ParseError, resolve_book_id

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

LANDING_API = "https://api.fqnovel.com/novel_ug/share/landing_page"
DIRECTORY_API = "https://fanqienovel.com/api/reader/directory/detail"
READER_PAGE = "https://fanqienovel.com/reader/{item_id}"
PC_PAGE = "https://fanqienovel.com/page/{book_id}"

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
    "Mobile/15E148 Safari/604.1"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DOWNLOAD_DIR = PROJECT_ROOT / "downloads"

# Windows 文件名非法字符
_ILLEGAL_FILENAME_RE = re.compile(r'[\\/:*?"<>|\r\n\t]+')
_TAG_RE = re.compile(r"<[^>]+>")
_P_RE = re.compile(r"<p(?:\s[^>]*)?>(.*?)</p>", re.IGNORECASE | re.DOTALL)
_WS_RE = re.compile(r"[ \t\r\f\v\u00a0]+")

ProgressCallback = Callable[[int, int, str], None]


class CrawlerError(Exception):
    """抓取过程中的可展示错误。"""


class ChapterUnavailable(CrawlerError):
    """单章不可抓取（App 独占、付费锁定、已下架等）。"""


class CaptchaError(CrawlerError):
    """触发了番茄的风控验证码中间页，需要冷却后再试。"""


# 验证码中间页特征（响应体里同时出现多个才判定，避免误伤正常页面）
_CAPTCHA_BYTES = (
    "验证码中间页".encode("utf-8"),
    b"middle_page_loading",
    b"sec_sdk_build",
)


def _is_captcha_page(content: bytes) -> bool:
    """判断响应是否为风控验证码中间页。"""
    if not content or len(content) > 80_000:
        return False
    if _CAPTCHA_BYTES[0] in content:
        return True
    lowered = content.lower()
    hits = sum(1 for marker in _CAPTCHA_BYTES[1:] if marker in lowered)
    return hits >= 2 and b"muye-reader-content" not in content


# --------------------------------------------------------------------------- #
# HTML 工具
# --------------------------------------------------------------------------- #

def _strip_tags(fragment: str) -> str:
    fragment = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.IGNORECASE)
    fragment = _TAG_RE.sub("", fragment)
    return html_lib.unescape(fragment)


def clean_paragraphs(html_fragment: str) -> list[str]:
    """把正文 HTML 片段拆成干净的段落列表（去标签 / 去空段 / 去零宽字符）。"""
    if not html_fragment:
        return []
    paragraphs: list[str] = []
    for raw in _P_RE.findall(html_fragment):
        text = _strip_tags(raw)
        text = text.replace("\u200b", "").replace("\ufeff", "")
        text = text.replace("\u3000", " ").strip()
        text = _WS_RE.sub(" ", text).strip()
        if text:
            paragraphs.append(text)
    if paragraphs:
        return paragraphs

    # 没有 <p> 标签时的兜底：按换行切分
    plain = _strip_tags(html_fragment)
    for line in plain.splitlines():
        line = line.replace("\u3000", " ").strip()
        if line:
            paragraphs.append(line)
    return paragraphs


def extract_reader_paragraphs(page_html: str) -> list[str]:
    """从 PC reader 页面 HTML 中提取正文段落。"""
    if not page_html:
        return []
    marker = "muye-reader-content noselect"
    index = page_html.find(marker)
    if index < 0:
        index = page_html.find("muye-reader-content")
    if index < 0:
        return []

    segment = page_html[index:index + 600_000]
    # 正文容器结构：<div class="muye-reader-content noselect"><div><p>..</p>..</div></div>
    end = segment.find("</div></div>")
    if end > 0:
        segment = segment[:end + len("</div>")]
    return clean_paragraphs(segment)


# --------------------------------------------------------------------------- #
# 抓取器
# --------------------------------------------------------------------------- #

class FanqieCrawler:
    """番茄小说抓取调度器（线程不安全，每个下载任务用一个实例即可）。"""

    def __init__(self, timeout: int = 20, retries: int = 3, delay: float = 0.3,
                 use_app_api: bool = True, app_api_interval: float = 6.0,
                 app_api: Optional[FanqieAppApi] = None):
        self.timeout = timeout
        self.retries = max(1, retries)
        self.delay = max(0.0, delay)
        # APP 正文接口：可下载全书（含 App 独占 / 会员章节），失败时回退网页通道
        self.use_app_api = bool(use_app_api)
        self.app_api_interval = max(0.0, float(app_api_interval))
        self._app_api: Optional[FanqieAppApi] = app_api
        self._app_api_error: Optional[str] = None
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": DESKTOP_UA,
            "Referer": "https://fanqienovel.com/",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
            "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                       "image/avif,image/webp,*/*;q=0.8"),
        })
        # 可选：用户自己的番茄登录 Cookie（环境变量 FANQIE_COOKIE），
        # 登录后可解锁会员章节的全文试读范围，未登录时只影响 VIP 章节。
        cookie = (os.environ.get("FANQIE_COOKIE") or "").strip()
        if cookie:
            self.session.headers["Cookie"] = cookie
        self._book_cache: dict[str, dict] = {}
        self._pc_available_cache: dict[str, bool] = {}

    # ---------------------------------------------------------------- 基础请求
    def _request(self, method: str, url: str, *, mobile: bool = False,
                 **kwargs) -> requests.Response:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("User-Agent", MOBILE_UA if mobile else DESKTOP_UA)
        if mobile:
            headers.setdefault("Referer", "https://changdunovel.com/")
        last_error: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                response = self.session.request(
                    method, url, timeout=self.timeout, headers=headers, **kwargs)
                if _is_captcha_page(response.content):
                    raise CaptchaError("触发番茄风控验证码，请稍后重试")
                if response.status_code in (429, 500, 502, 503, 504):
                    last_error = CrawlerError(
                        f"服务端返回 {response.status_code}")
                    time.sleep(0.6 * (attempt + 1))
                    continue
                return response
            except CaptchaError:
                raise
            except requests.exceptions.RequestException as exc:
                last_error = exc
                time.sleep(0.5 * (attempt + 1))
        raise CrawlerError(f"网络请求失败：{last_error}") from None

    def _get_json(self, url: str, *, mobile: bool = False, **kwargs) -> dict:
        response = self._request("GET", url, mobile=mobile, **kwargs)
        try:
            return response.json()
        except ValueError:
            raise CrawlerError("接口返回了非 JSON 数据，可能触发了风控") from None

    def _get_app_api(self) -> FanqieAppApi:
        """惰性创建 APP 正文客户端（首次调用会注册匿名设备）。"""
        if self._app_api is None:
            self._app_api = FanqieAppApi(timeout=max(20, self.timeout),
                                         interval=self.app_api_interval)
        return self._app_api

    # ---------------------------------------------------------------- 元数据
    def _fetch_landing_data(self, book_id: str) -> dict:
        """请求落地页接口，返回 book_data（含前 10 章明文）。"""
        if book_id in self._book_cache:
            return self._book_cache[book_id]
        params = {
            "aid": 1967,
            "series_id": 0,
            "performance_optimization": 1,
            "share_type": 11,
            "book_id": book_id,
        }
        payload = self._get_json(LANDING_API, mobile=True, params=params)
        err_no = payload.get("err_no")
        if err_no not in (None, 0):
            raise CrawlerError(
                f"书籍信息获取失败（err_no={err_no}）：{payload.get('err_tips') or ''}")
        book_data = (payload.get("data") or {}).get("book_data")
        if not book_data or not book_data.get("title"):
            raise CrawlerError("没有找到这本书，可能已下架或 book_id 不正确")
        self._book_cache[book_id] = book_data
        return book_data

    def check_pc_available(self, book_id: str) -> bool:
        """PC 端是否有阅读页（App 独占书返回 False）。"""
        if book_id in self._pc_available_cache:
            return self._pc_available_cache[book_id]
        available = False
        try:
            response = self._request("GET", PC_PAGE.format(book_id=book_id))
            available = (response.status_code == 200
                         and "chapter-item" in response.text)
        except CrawlerError:
            available = False
        self._pc_available_cache[book_id] = available
        return available

    def fetch_metadata(self, book_id: str) -> dict:
        """获取书籍元数据（解析接口用）。"""
        book_data = self._fetch_landing_data(book_id)
        author = book_data.get("author") or {}
        try:
            chapter_count = int(book_data.get("serial_count") or 0)
        except (TypeError, ValueError):
            chapter_count = 0
        try:
            word_count = int(book_data.get("word_count") or 0)
        except (TypeError, ValueError):
            word_count = 0
        creation_status = book_data.get("creation_status")
        serial_status = {0: "已完结", 1: "连载中"}.get(creation_status, "未知状态")
        first_chapters = book_data.get("first_chapters_content") or []
        # 启用 APP 接口时可下载全书；否则只有落地页前 10 章
        available_chapters = chapter_count if (self.use_app_api and chapter_count) \
            else len(first_chapters)
        return {
            "book_id": book_id,
            "title": book_data.get("title") or "未知书名",
            "author": author.get("name") or "未知作者",
            "score": str(book_data.get("score") or "暂无"),
            "cover_url": book_data.get("cover_url") or "",
            "intro": (book_data.get("intro") or "").strip(),
            "word_count": word_count,
            "chapter_count": chapter_count,
            "serial_status": serial_status,
            "available_chapters": available_chapters,
            "pc_available": self.check_pc_available(book_id),
            "chapter_titles": list(book_data.get("item_names") or []),
            "category_tags": list(book_data.get("category_tags") or []),
            "reading_count_text": book_data.get("reading_count_text") or "",
        }

    # ---------------------------------------------------------------- 目录
    def fetch_chapter_list(self, book_id: str) -> list[dict]:
        """获取全书章节列表，元素：``{index, item_id, title, locked}``。"""
        try:
            payload = self._get_json(DIRECTORY_API,
                                     params={"bookId": book_id})
        except CrawlerError:
            payload = {}

        data = payload.get("data") or {}
        chapters: list[dict] = []
        for volume in data.get("chapterListWithVolume") or []:
            for item in volume or []:
                item_id = str(item.get("itemId") or "")
                if not item_id:
                    continue
                chapters.append({
                    "index": len(chapters) + 1,
                    "item_id": item_id,
                    "title": (item.get("title") or "").strip(),
                    "locked": bool(item.get("isChapterLock")
                                   or item.get("needPay")
                                   or item.get("isPaidPublication")),
                })

        # 目录接口偶发只给 allItemIds 时兜底
        if not chapters:
            all_ids = data.get("allItemIds") or []
            landing = self._book_cache.get(book_id) or {}
            titles = landing.get("item_names") or []
            for i, item_id in enumerate(all_ids):
                chapters.append({
                    "index": i + 1,
                    "item_id": str(item_id),
                    "title": titles[i] if i < len(titles) else f"第{i + 1}章",
                    "locked": False,
                })

        if not chapters:
            # 目录彻底失败：至少让落地页前 10 章可下载
            landing = self._fetch_landing_data(book_id)
            for i, title in enumerate(landing.get("item_names") or []):
                chapters.append({"index": i + 1, "item_id": None,
                                 "title": title, "locked": False})
        return chapters

    # ---------------------------------------------------------------- 正文
    def fetch_chapter_text(self, item_id: str) -> str:
        """抓取单章并解密，返回以 ``\\n`` 连接的段落文本。"""
        if not item_id:
            raise ChapterUnavailable("缺少章节 item_id")
        url = READER_PAGE.format(item_id=item_id)
        response = self._request("GET", url)
        if response.status_code == 404:
            raise ChapterUnavailable("PC 端未上架（可能为 App 独占章节）")
        if response.status_code != 200:
            raise CrawlerError(f"章节页返回 {response.status_code}")

        # 会员 / SVIP 章节：页面只给几段试读并提示下载 App / 登录，免登录拿不到全文
        if ("muye-to-vip" in response.text
                or "扫码下载APP免费读" in response.text
                or "会员登录后" in response.text):
            raise ChapterUnavailable("会员/付费章节，免登录模式无法获取全文")

        paragraphs = extract_reader_paragraphs(response.text)
        if not paragraphs:
            if "no-content" in response.text:
                raise ChapterUnavailable("PC 端暂无正文（App 独占）")
            raise CrawlerError("章节正文为空")

        plain = decrypt_text("\n".join(paragraphs))
        leftover = remaining_pua(plain)
        if leftover:
            raise CrawlerError(
                f"字体映射未覆盖 {len(leftover)} 个字符，可能是字体已更新")
        return plain

    # ---------------------------------------------------------------- 下载
    def download_book(self, book_id: str,
                      progress_callback: Optional[ProgressCallback] = None,
                      output_dir: Optional[str | os.PathLike] = None) -> dict:
        """下载整本书并导出 TXT。

        Args:
            book_id: 番茄 book_id。
            progress_callback: ``callback(current, total, chapter_title)``，
                每完成一章回调一次（失败章节也会回调，便于前端展示进度）。
            output_dir: 导出目录，默认项目根目录 ``downloads/``。

        Returns:
            dict: ``{success, filename, path, title, total, downloaded, failed,
            failed_chapters, pc_available, app_api_used, app_api_error, message}``
        """
        metadata = self.fetch_metadata(book_id)
        title = metadata["title"]
        book_data = self._fetch_landing_data(book_id)
        landing_html = book_data.get("first_chapters_content") or []
        landing_chapters = [clean_paragraphs(fragment) for fragment in landing_html]
        landing_available = len(landing_chapters)

        chapters = self.fetch_chapter_list(book_id)
        total = max(len(chapters), landing_available, metadata["chapter_count"], 1)
        if len(chapters) < total:
            # 目录不全时用落地页章节标题补齐（正文仍按落地页处理）
            titles = book_data.get("item_names") or []
            for i in range(len(chapters), total):
                chapters.append({
                    "index": i + 1,
                    "item_id": None,
                    "title": titles[i] if i < len(titles) else f"第{i + 1}章",
                    "locked": False,
                })

        pc_available = metadata["pc_available"]
        landing_available = len(landing_chapters)

        if progress_callback:
            try:
                progress_callback(0, total, "正在准备下载…")
            except Exception:
                pass

        # ------------------------------------------------------------------
        # 通道一：APP 接口（可下全书，含 App 独占 / 会员章节）
        # ------------------------------------------------------------------
        app_paragraphs: dict[str, list[str]] = {}
        app_titles: dict[str, str] = {}
        app_emitted: set[str] = set()
        app_used = False
        app_error: Optional[str] = None

        if self.use_app_api:
            item_ids = [c["item_id"] for c in chapters if c.get("item_id")]
            chapter_by_id = {c["item_id"]: c for c in chapters if c.get("item_id")}
            if item_ids:
                try:
                    api = self._get_app_api()

                    def on_batch(batch: list[str], got: dict[str, str]) -> None:
                        for item_id in batch:
                            chapter = chapter_by_id.get(item_id)
                            if not chapter or item_id not in got:
                                continue
                            title_text, paragraphs = parse_chapter_xhtml(got[item_id])
                            if not paragraphs:
                                continue
                            app_paragraphs[item_id] = paragraphs
                            if title_text:
                                app_titles[item_id] = title_text
                            if progress_callback and item_id not in app_emitted:
                                app_emitted.add(item_id)
                                try:
                                    progress_callback(chapter["index"], total,
                                                      chapter["title"] or title_text)
                                except Exception:
                                    pass

                    raw = api.fetch_chapters(item_ids, book_id, on_batch=on_batch)
                    for item_id, xhtml in raw.items():
                        if item_id in app_paragraphs:
                            continue
                        title_text, paragraphs = parse_chapter_xhtml(xhtml)
                        if paragraphs:
                            app_paragraphs[item_id] = paragraphs
                            if title_text:
                                app_titles[item_id] = title_text
                    app_used = bool(app_paragraphs)
                except AppApiError as exc:
                    app_error = str(exc)
                    self._app_api_error = app_error
                except Exception as exc:  # 兜底：任何异常都不阻断网页通道
                    app_error = f"{type(exc).__name__}: {exc}"
                    self._app_api_error = app_error

        # ------------------------------------------------------------------
        # 通道二：网页通道（落地页前 10 章明文 + PC reader）
        # ------------------------------------------------------------------
        app_only_skipped = False
        pc_404_streak = 0
        vip_gate_streak = 0
        vip_gate_detected = False
        captcha_hits = 0
        captcha_stopped = False

        output: list[str] = []
        failed: list[dict] = []
        downloaded = 0
        locked_failed = 0

        for chapter in chapters:
            index = chapter["index"]
            chapter_title = chapter["title"] or f"第{index}章"
            item_id = chapter.get("item_id")
            paragraphs: list[str] = []
            error: Optional[str] = None

            if item_id and item_id in app_paragraphs:
                # APP 接口已拿到正文（明文，无需字体解密）
                paragraphs = app_paragraphs[item_id]
                if app_titles.get(item_id):
                    chapter_title = app_titles[item_id]
            elif index <= landing_available and landing_chapters[index - 1]:
                # 落地页明文通道（前 10 章）
                paragraphs = landing_chapters[index - 1]
            elif captcha_stopped:
                error = "触发番茄风控验证码，已跳过（请稍后重试）"
            elif app_only_skipped:
                error = "该书为 App 独占，PC 端无正文"
            elif vip_gate_detected and chapter.get("locked"):
                # 已确认整本书的 VIP 章节被拦截：剩余锁定章节直接标记，不再空跑
                error = "会员/付费章节，免登录模式无法获取全文"
                locked_failed += 1
            elif item_id:
                if not pc_available:
                    pc_404_streak += 1
                    if pc_404_streak >= 2:
                        app_only_skipped = True
                try:
                    text = self.fetch_chapter_text(item_id)
                    paragraphs = [line for line in text.split("\n") if line.strip()]
                    pc_404_streak = 0
                    vip_gate_streak = 0
                except ChapterUnavailable as exc:
                    error = str(exc)
                    if "会员" in error or "付费" in error:
                        locked_failed += 1
                        vip_gate_streak += 1
                        if vip_gate_streak >= 3:
                            vip_gate_detected = True
                    if not pc_available:
                        pc_404_streak += 1
                        if pc_404_streak >= 2:
                            app_only_skipped = True
                except CaptchaError as exc:
                    captcha_hits += 1
                    if captcha_hits >= 2:
                        captcha_stopped = True
                        error = "触发番茄风控验证码，已停止下载，请稍后重试"
                    else:
                        error = str(exc)
                        time.sleep(8.0)  # 冷却后下一章再试
                except CrawlerError as exc:
                    error = str(exc)
                time.sleep(self.delay + random.uniform(0, 0.06))
            else:
                error = "该章节没有可用的正文来源"

            if paragraphs:
                downloaded += 1
                output.append(self._format_chapter(chapter_title, paragraphs))
            else:
                failed.append({"index": index, "title": chapter_title,
                               "reason": error or "内容为空"})

            if progress_callback and item_id not in app_emitted:
                try:
                    progress_callback(index, total, chapter_title)
                except Exception:
                    pass

        filename = self._safe_filename(title) + ".txt"
        target_dir = Path(output_dir) if output_dir else DEFAULT_DOWNLOAD_DIR
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / filename

        content = self._format_book(metadata, output, downloaded, total,
                                    landing_available, failed)
        path.write_text(content, encoding="utf-8-sig", newline="\n")

        if captcha_stopped:
            message = (f"下载中断：触发番茄风控验证码，已导出 {downloaded}/{total} 章，"
                       f"请等待几分钟后重试")
            success = downloaded > 0
        elif downloaded == 0:
            if app_error:
                message = f"APP 接口不可用（{app_error}），网页接口也没有抓到正文，请稍后重试"
            else:
                message = "没有抓到任何章节正文，请稍后重试或换一本书"
            success = False
        elif downloaded >= total:
            message = f"下载完成，共 {downloaded} 章"
            success = True
        elif app_used:
            message = (f"已导出 {downloaded}/{total} 章，"
                       f"{len(failed)} 章未获取，可稍后重试失败章节")
            success = True
        elif not pc_available:
            message = (f"该书为 App 独占，PC 端仅提供前 {landing_available} 章试读，"
                       f"已导出可获取的 {downloaded} 章")
            success = True
        elif locked_failed and locked_failed == len(failed):
            message = (f"已导出 {downloaded}/{total} 章，"
                       f"{locked_failed} 章为会员/付费章节（免登录无法获取）")
            success = True
        elif locked_failed:
            message = (f"已导出 {downloaded}/{total} 章，"
                       f"{len(failed)} 章不可用（其中 {locked_failed} 章为会员/付费章节）")
            success = True
        else:
            message = f"下载完成 {downloaded}/{total} 章，{len(failed)} 章失败"
            success = True

        if app_error and downloaded > 0 and not captcha_stopped:
            message = f"APP 接口不可用（{app_error}），已回退网页接口：{message}"

        return {
            "success": success,
            "filename": filename,
            "path": str(path),
            "title": title,
            "total": total,
            "downloaded": downloaded,
            "failed": len(failed),
            "failed_chapters": failed[:50],
            "pc_available": pc_available,
            "app_api_used": app_used,
            "app_api_error": app_error,
            "message": message,
        }

    # ---------------------------------------------------------------- 排版
    @staticmethod
    def _format_chapter(title: str, paragraphs: Iterable[str]) -> str:
        body = "\n\n".join(f"\u3000\u3000{para.strip()}" for para in paragraphs
                           if para.strip())
        return f"【{title}】\n\n{body}\n\n"

    @staticmethod
    def _format_book(metadata: dict, chapters: list[str], downloaded: int,
                     total: int, landing_available: int,
                     failed: list[dict]) -> str:
        word_count = metadata.get("word_count") or 0
        if word_count >= 100_000_000:
            word_text = f"{word_count / 100_000_000:.2f} 亿字"
        elif word_count >= 10_000:
            word_text = f"{word_count / 10_000:.1f} 万字"
        else:
            word_text = f"{word_count} 字"

        exported = f"{downloaded}/{total} 章"
        if failed and downloaded < total:
            exported += f"（{len(failed)} 章不可用）"

        lines = [
            "=" * 46,
            f"书名：{metadata.get('title', '未知书名')}",
            f"作者：{metadata.get('author', '未知作者')}",
            f"评分：{metadata.get('score', '暂无')}",
            f"字数：{word_text}",
            f"状态：{metadata.get('serial_status', '未知')}",
            f"章节：共 {total} 章，已导出 {exported}",
            "=" * 46,
            "",
        ]
        intro = (metadata.get("intro") or "").strip()
        if intro:
            lines.extend(["【内容简介】", intro, "", "-" * 46, ""])
        lines.extend(chapters)
        lines.append("=" * 46)
        lines.append("由 fanqie-novel-downloader 导出")
        lines.append("=" * 46)
        return "\n".join(lines)

    @staticmethod
    def _safe_filename(name: str) -> str:
        cleaned = _ILLEGAL_FILENAME_RE.sub("_", name or "未命名").strip(" .")
        cleaned = cleaned[:80] or "未命名"
        return cleaned


# --------------------------------------------------------------------------- #
# 便捷函数
# --------------------------------------------------------------------------- #

def download_book(book_id: str,
                  progress_callback: Optional[ProgressCallback] = None,
                  output_dir: Optional[str | os.PathLike] = None) -> dict:
    """模块级快捷入口，内部自建 :class:`FanqieCrawler`。"""
    return FanqieCrawler().download_book(
        book_id, progress_callback=progress_callback, output_dir=output_dir)


def download_by_input(text: str,
                      progress_callback: Optional[ProgressCallback] = None,
                      output_dir: Optional[str | os.PathLike] = None) -> dict:
    """从分享文案 / 链接一步到位下载。"""
    book_id = resolve_book_id(text)
    return download_book(book_id, progress_callback, output_dir)


# --------------------------------------------------------------------------- #
# 自测：python core/crawler.py
# --------------------------------------------------------------------------- #

def _self_test() -> int:
    book_id = sys.argv[1] if len(sys.argv) > 1 else "7527891257914297368"
    crawler = FanqieCrawler()
    try:
        metadata = crawler.fetch_metadata(book_id)
    except (CrawlerError, ParseError) as exc:
        print(f"[FAIL] 元数据获取失败：{exc}")
        return 1

    print("书名：", metadata["title"])
    print("作者：", metadata["author"])
    print("评分：", metadata["score"])
    print("字数：", metadata["word_count"])
    print("章节数：", metadata["chapter_count"])
    print("PC 可读：", metadata["pc_available"])
    print("可下载章节：", metadata["available_chapters"])
    print("-" * 60)

    book_data = crawler._fetch_landing_data(book_id)
    fragments = book_data.get("first_chapters_content") or []
    if not fragments:
        print("[FAIL] 落地页没有返回前 10 章正文")
        return 1

    total_pua = 0
    for i, fragment in enumerate(fragments[:10]):
        paragraphs = clean_paragraphs(fragment)
        text = decrypt_text("\n".join(paragraphs))
        leftover = remaining_pua(text)
        total_pua += len(leftover)
        head = text.replace("\n", " ")[:60]
        print(f"第 {i + 1} 章：{len(paragraphs)} 段 | {head}…")
        if leftover:
            print(f"  [WARN] 仍有 PUA 字符：{[hex(ord(c)) for c in leftover[:5]]}")

    if total_pua:
        print(f"[FAIL] 解密后仍有 {total_pua} 个 PUA 字符")
        return 1
    print("-" * 60)
    print("[OK] 前 10 章明文获取与字体解密正常，无 PUA 残留")

    # APP 正文通道：抽 3 章验证（App 独占 / 会员章节也走这里）
    try:
        api = crawler._get_app_api()
        api.ensure_ready()
        print(f"[OK] APP 匿名设备就绪：{api.device['device_id']}，"
              f"密钥版本 {api.key_version}")
        chapters = crawler.fetch_chapter_list(book_id)
        ids = [c["item_id"] for c in chapters[:3] if c.get("item_id")]
        raw = api.fetch_chapters(ids, book_id) if ids else {}
        if not raw:
            print("[FAIL] APP 接口没有返回正文")
            return 1
        app_pua = 0
        for item_id in ids:
            title_text, paragraphs = parse_chapter_xhtml(raw.get(item_id, ""))
            text = "\n".join(paragraphs)
            app_pua += len(remaining_pua(text))
            print(f"  APP 第 {chapters[ids.index(item_id)]['index']} 章："
                  f"{title_text} | {len(paragraphs)} 段")
        if app_pua:
            print(f"[FAIL] APP 正文仍有 {app_pua} 个 PUA 字符")
            return 1
        print("[OK] APP 接口正文获取与解密正常（可下全书）")
    except AppApiError as exc:
        print(f"[WARN] APP 接口不可用，将回退网页通道：{exc}")
    print("-" * 60)
    print("[OK] 自测通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
