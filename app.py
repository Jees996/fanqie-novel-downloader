#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""番茄小说一键下载器 —— FastAPI 后端服务。

接口一览:
    POST /api/parse                    解析分享链接/文本 -> 书籍元信息
    GET  /api/download/progress        SSE 下载进度（首个订阅者触发后台下载线程）
    GET  /api/download/file/{name}     下载导出的 TXT 文件
    GET  /api/health                   健康检查
    GET  /                             返回 static/index.html
    GET  /static/*                     静态资源

运行:
    python app.py
    默认监听 127.0.0.1:8000，可用环境变量 PORT 覆盖；FANQIE_NO_BROWSER=1 可关闭自动打开浏览器。
"""

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Dict

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# 基础路径（全部基于 app.py 所在目录，保证任意工作目录下启动都正确）
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

STATIC_DIR = BASE_DIR / "static"
DOWNLOAD_DIR = BASE_DIR / "downloads"
INDEX_FILE = STATIC_DIR / "index.html"

VERSION = "1.1.0"
REPO_URL = "https://github.com/Jees996/fanqie-novel-downloader"
REPO_ISSUES_URL = REPO_URL + "/issues"
HOST = "127.0.0.1"
DEFAULT_PORT = 8000

SSE_POLL_SECONDS = 0.4      # SSE 轮询共享任务状态的间隔
SSE_HEARTBEAT_SECONDS = 15.0  # 无更新时的心跳注释间隔
TERMINAL_STATUSES = {"done", "error"}
BOOK_ID_RE = re.compile(r"^[0-9A-Za-z_-]{1,64}$")
CRAWLER_TIMEOUT = 20
CRAWLER_RETRIES = 3


def _env_float(name: str, default: float) -> float:
    """读取浮点型环境变量，非法值回退默认值。"""
    try:
        raw = os.environ.get(name, "")
        return float(raw) if str(raw).strip() else default
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    """读取整型环境变量，非法值回退默认值。"""
    try:
        raw = os.environ.get(name, "")
        return int(str(raw).strip()) if str(raw).strip() else default
    except (TypeError, ValueError):
        return default


# APP 正文通道（可下全书）：FANQIE_NO_APP_API=1 可强制回退网页通道
APP_API_ENABLED = (os.environ.get("FANQIE_NO_APP_API", "").strip().lower()
                   not in {"1", "true", "yes", "on"})
# 相邻两批章节的最小间隔（秒）；过低会被番茄网关限流
APP_API_INTERVAL = _env_float("FANQIE_APP_INTERVAL", 6.0)


# /api/parse 的整体超时保护（爬虫自身 20s x 3 次重试，这里留出余量）
# 默认 40s，略小于前端 fetch 的 45s 超时，保证前端能收到后端的中文超时提示。
PARSE_TIMEOUT_SECONDS = max(10.0, _env_float("FANQIE_PARSE_TIMEOUT", 40.0))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("fanqie.app")

# ---------------------------------------------------------------------------
# 核心模块导入（解析器 + 爬虫）
# core/ 由核心模块提供；这里统一捕获导入异常，避免用户看到原始 traceback。
# ---------------------------------------------------------------------------
CORE_READY = False
CORE_LOAD_ERROR = ""

try:
    from core.parser import ParseError, resolve_book_id
    from core.crawler import CrawlerError, FanqieCrawler

    CORE_READY = True
except Exception as _core_exc:  # 仅在 core 缺失或依赖未安装时触发
    CORE_LOAD_ERROR = f"核心模块加载失败：{_core_exc}"
    print("=" * 62, file=sys.stderr)
    print("  [错误] 核心模块 (core/parser.py, core/crawler.py) 加载失败。", file=sys.stderr)
    print(f"  原因: {_core_exc}", file=sys.stderr)
    print("  请确认 core/ 目录完整，并已执行 pip install -r requirements.txt。", file=sys.stderr)
    print("=" * 62, file=sys.stderr)

    class _CoreUnavailableError(Exception):
        """核心模块不可用时的占位异常。"""

    ParseError = _CoreUnavailableError  # type: ignore[assignment,misc]
    CrawlerError = _CoreUnavailableError  # type: ignore[assignment,misc]

    def resolve_book_id(text: str) -> str:  # type: ignore[misc]
        raise _CoreUnavailableError(CORE_LOAD_ERROR)

    class FanqieCrawler:  # type: ignore[no-redef]
        """核心模块不可用时的占位类，构造即报错。"""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise _CoreUnavailableError(CORE_LOAD_ERROR)


# ---------------------------------------------------------------------------
# 下载任务状态（同一 book_id 全局唯一，多个 SSE 订阅者共享）
# ---------------------------------------------------------------------------
JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()


def _now() -> float:
    return time.time()


def _new_job(book_id: str) -> Dict[str, Any]:
    """创建一个初始任务状态。"""
    return {
        "book_id": book_id,
        "status": "start",
        "current": 0,
        "total": 0,
        "title": "",
        "chapter_title": "",
        "percent": 0,
        "filename": None,
        "failed": 0,
        "message": "开始下载",
        "seq": 0,
        "events": [],  # 历史进度快照，保证每个章节进度都能推送给订阅者
        "created_at": _now(),
        "updated_at": _now(),
    }


def _safe_int(value: Any, default: int = 0) -> int:
    """把任意值安全转换为 int。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _update_job(book_id: str, **fields: Any) -> None:
    """线程安全地更新任务状态；终态之后忽略迟到的非终态更新。"""
    with JOBS_LOCK:
        job = JOBS.get(book_id)
        if job is None:
            return

        incoming_status = fields.get("status")
        if job["status"] in TERMINAL_STATUSES and incoming_status not in TERMINAL_STATUSES:
            return

        job.update(fields)
        job["current"] = max(0, _safe_int(job.get("current"), 0))
        job["total"] = max(0, _safe_int(job.get("total"), 0))
        job["failed"] = max(0, _safe_int(job.get("failed"), 0))

        if job["status"] == "done":
            job["percent"] = 100
        elif job["total"] > 0:
            job["percent"] = max(
                0, min(100, int(round(job["current"] * 100.0 / job["total"])))
            )
        else:
            job["percent"] = 0

        job["seq"] = _safe_int(job.get("seq"), 0) + 1
        job["updated_at"] = _now()
        # 记录本次状态快照：SSE 订阅者按索引逐条推送，避免 0.4s 轮询吞掉快速章节
        events = job.get("events")
        if not isinstance(events, list):
            events = job["events"] = []
        events.append(_public_job_payload(job))


def _public_job_payload(job: Dict[str, Any]) -> Dict[str, Any]:
    """组装推送给前端的 JSON（字段与前端约定保持一致）。"""
    return {
        "book_id": job.get("book_id", ""),
        "status": job.get("status", "error"),
        "current": _safe_int(job.get("current"), 0),
        "total": _safe_int(job.get("total"), 0),
        "title": job.get("title") or "",
        "chapter_title": job.get("chapter_title") or "",
        "percent": _safe_int(job.get("percent"), 0),
        "filename": job.get("filename"),
        "failed": _safe_int(job.get("failed"), 0),
        "message": job.get("message") or "",
    }


def _sse_pack(payload: Dict[str, Any]) -> str:
    """序列化为 SSE data 帧。"""
    return "data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n\n"


def _error_payload(book_id: str, message: str) -> Dict[str, Any]:
    payload = _public_job_payload(_new_job(book_id))
    payload["status"] = "error"
    payload["message"] = message
    return payload


def _friendly_error(exc: BaseException, fallback: str) -> str:
    """把异常转成中文提示；爬虫异常本身通常已是中文。"""
    text = str(exc).strip()
    if isinstance(exc, CrawlerError) and text:
        return text
    if text:
        return f"{fallback}：{text}"
    return fallback


# ---------------------------------------------------------------------------
# 下载线程
# ---------------------------------------------------------------------------
def _build_crawler() -> Any:
    return FanqieCrawler(timeout=CRAWLER_TIMEOUT, retries=CRAWLER_RETRIES,
                         use_app_api=APP_API_ENABLED,
                         app_api_interval=APP_API_INTERVAL)


def _download_worker(book_id: str) -> None:
    """后台下载线程：调用 crawler.download_book，实时刷新共享任务状态。"""
    try:
        _update_job(book_id, status="downloading", message="正在获取章节目录…")

        try:
            crawler = _build_crawler()
        except Exception as exc:
            _update_job(book_id, status="error", message=_friendly_error(exc, "初始化下载器失败"))
            return

        def on_progress(current: int, total: int, title: str) -> None:
            cur = max(0, _safe_int(current, 0))
            tot = max(0, _safe_int(total, 0))
            chapter_title = str(title or "").strip()
            if cur <= 0:
                message = chapter_title or "正在准备下载…"
            else:
                message = f"正在下载：第 {cur}/{tot} 章"
                if chapter_title:
                    message = f"正在下载：第 {cur}/{tot} 章 - {chapter_title}"
            _update_job(
                book_id,
                status="downloading",
                current=cur,
                total=tot,
                title=chapter_title,
                chapter_title=chapter_title,
                message=message,
            )

        try:
            result = crawler.download_book(
                book_id,
                progress_callback=on_progress,
                output_dir=str(DOWNLOAD_DIR),
            )
        except CrawlerError as exc:
            logger.warning("下载失败(book_id=%s): %s", book_id, exc)
            _update_job(book_id, status="error", message=_friendly_error(exc, "下载失败，请稍后重试"))
            return
        except Exception as exc:
            logger.exception("下载异常(book_id=%s)", book_id)
            _update_job(book_id, status="error", message=_friendly_error(exc, "下载出现异常，请稍后重试"))
            return

        if not isinstance(result, dict):
            _update_job(book_id, status="error", message="下载器返回了无效结果，请重试")
            return

        failed = max(0, _safe_int(result.get("failed"), 0))
        if not result.get("success"):
            message = str(result.get("message") or "").strip() or "下载失败，请稍后重试"
            _update_job(book_id, status="error", failed=failed, message=message)
            return

        filename = Path(str(result.get("filename") or "").strip()).name
        path_value = result.get("path")
        source_path = Path(str(path_value)) if path_value else None
        # 防御性处理：核心模块若把文件写到其它目录（或返回带路径的文件名），
        # 统一复制/归一到 downloads/，保证 /api/download/file 一定能取到。
        try:
            if source_path is not None and source_path.is_file():
                if source_path.parent.resolve() != DOWNLOAD_DIR.resolve():
                    target_path = DOWNLOAD_DIR / (filename or source_path.name)
                    if not target_path.exists():
                        shutil.copyfile(str(source_path), str(target_path))
                    filename = target_path.name
                elif not filename:
                    filename = source_path.name
        except OSError as copy_exc:
            logger.warning("复制下载文件到下载目录失败: %s", copy_exc)

        if not filename:
            _update_job(book_id, status="error", message="下载完成但未返回文件名，请重试")
            return
        if not (DOWNLOAD_DIR / filename).is_file():
            _update_job(book_id, status="error", message="下载完成但未在下载目录找到文件，请重试")
            return

        total = _safe_int(result.get("total"), 0)
        downloaded = _safe_int(result.get("downloaded"), total)
        book_title = str(result.get("title") or "").strip()
        message = str(result.get("message") or "").strip() or f"下载完成，共 {downloaded} 章"

        fields: Dict[str, Any] = {
            "status": "done",
            "current": downloaded,
            "total": total,
            "filename": filename,
            "failed": failed,
            "message": message,
        }
        if book_title:
            fields["title"] = book_title
        _update_job(book_id, **fields)
    except Exception as exc:  # 兜底：线程绝不能悄无声息地挂掉
        logger.exception("下载线程异常退出(book_id=%s)", book_id)
        _update_job(book_id, status="error", message=_friendly_error(exc, "下载线程异常退出"))


def _ensure_job(book_id: str) -> None:
    """保证 book_id 有且只有一个任务；首次调用时启动后台下载线程。

    语义：
    * 任务进行中  -> 只订阅，不重复下载；
    * 任务已完成  -> 直接回放结果，不重跑；
    * 任务已失败  -> 重置任务并重跑（前端「重试下载」只是重新订阅同一个
      book_id，必须支持失败后重试）。
    """
    with JOBS_LOCK:
        existing = JOBS.get(book_id)
        if existing is not None and existing.get("status") not in TERMINAL_STATUSES:
            return  # 正在跑：只订阅
        if existing is not None and existing.get("status") == "done":
            # 完整成功的任务直接回放；部分章节失败（App 独占 / 风控中断 /
            # 会员章节）则允许重新订阅触发重试，避免用户必须重启服务。
            if _safe_int(existing.get("failed"), 0) == 0:
                return
        job = _new_job(book_id)
        if not CORE_READY:
            job["status"] = "error"
            job["message"] = CORE_LOAD_ERROR or "核心模块不可用，无法下载"
            job["seq"] = 1
            job["updated_at"] = _now()
            JOBS[book_id] = job
            return
        JOBS[book_id] = job

    thread = threading.Thread(
        target=_download_worker,
        args=(book_id,),
        name=f"fanqie-download-{book_id}",
        daemon=True,
    )
    try:
        thread.start()
    except Exception as exc:
        logger.exception("启动下载线程失败(book_id=%s)", book_id)
        _update_job(book_id, status="error", message=_friendly_error(exc, "启动下载线程失败"))


# ---------------------------------------------------------------------------
# 超时保护工具：在守护线程里执行阻塞调用，超时则放弃等待
# ---------------------------------------------------------------------------
class UpstreamTimeout(Exception):
    """上游请求超时。"""


def _run_with_timeout(func: Any, timeout_seconds: float) -> Any:
    box: Dict[str, Any] = {}
    done = threading.Event()

    def _runner() -> None:
        try:
            box["value"] = func()
        except BaseException as exc:  # noqa: BLE001 - 原样抛回调用方
            box["error"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=_runner, name="fanqie-parse", daemon=True)
    thread.start()
    if not done.wait(timeout_seconds):
        raise UpstreamTimeout("上游请求超时")
    if "error" in box:
        raise box["error"]
    return box.get("value")


# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------
class ParseRequest(BaseModel):
    url: str


app = FastAPI(
    title="番茄小说一键下载器",
    description="番茄小说解析 / 全本下载 / TXT 导出",
    version=VERSION,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

# 下载目录（模块导入时即创建，确保接口随时可用）
try:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
except OSError as _mkdir_exc:
    print(f"[警告] 无法创建下载目录 {DOWNLOAD_DIR}: {_mkdir_exc}", file=sys.stderr)

# 静态资源目录（存在才挂载）
if STATIC_DIR.is_dir():
    try:
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    except Exception as _mount_exc:  # pragma: no cover
        logger.warning("静态目录挂载失败: %s", _mount_exc)

_MISSING_INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>番茄小说一键下载器</title></head>
<body style="margin:0;padding:48px;background:#0a0a0f;color:#e5e7eb;font-family:system-ui,-apple-system,'Segoe UI',sans-serif;">
  <h2 style="margin:0 0 12px;">🍅 番茄小说一键下载器</h2>
  <p style="color:#9ca3af;">前端页面 <code style="color:#f97316;">static/index.html</code> 尚未就绪。</p>
  <p style="color:#9ca3af;">后端接口已可用：<a style="color:#f97316;" href="/api/health">/api/health</a></p>
</body>
</html>"""

_METADATA_DEFAULT_KEYS = (
    "title",
    "author",
    "score",
    "cover_url",
    "intro",
    "word_count",
    "chapter_count",
    "serial_status",
    "available_chapters",
    "pc_available",
)


def _fill_metadata_defaults(metadata: Dict[str, Any]) -> None:
    """补齐可能缺失的字段，避免前端渲染时取值异常。"""
    for key in _METADATA_DEFAULT_KEYS:
        if key not in metadata:
            metadata[key] = 0 if key in ("word_count", "chapter_count", "available_chapters") else ""
    if "pc_available" not in metadata:
        metadata["pc_available"] = False
    if "chapter_titles" not in metadata:
        metadata["chapter_titles"] = []


# ------------------------------- 健康检查 -------------------------------
@app.get("/api/health")
def api_health() -> Dict[str, Any]:
    return {"ok": True, "version": VERSION, "core_ready": CORE_READY}


# ------------------------------- 解析书籍 -------------------------------
@app.post("/api/parse")
def api_parse(payload: ParseRequest) -> Any:
    """解析分享文本/链接，返回书籍元信息。可预期的错误一律 HTTP 200 + 中文提示。"""
    if not CORE_READY:
        return JSONResponse(
            status_code=200,
            content={"success": False, "error": CORE_LOAD_ERROR or "核心模块未就绪，无法解析书籍"},
        )

    raw = str(payload.url or "").strip()
    if not raw:
        return {"success": False, "error": "请输入番茄小说链接或书籍 ID"}
    if len(raw) > 4000:
        return {"success": False, "error": "输入内容过长，请检查后重试"}

    try:
        book_id = str(resolve_book_id(raw) or "").strip()
    except ParseError as exc:
        message = str(exc).strip() or "无法识别番茄小说链接，请检查后重试"
        return {"success": False, "error": message}
    except Exception as exc:
        logger.exception("解析链接异常")
        return {"success": False, "error": _friendly_error(exc, "解析链接失败，请检查链接是否有效")}

    if not book_id:
        return {"success": False, "error": "未能识别书籍 ID，请检查链接"}
    if not BOOK_ID_RE.match(book_id):
        return {"success": False, "error": "识别到的书籍 ID 格式不正确，请检查链接"}

    try:
        metadata = _run_with_timeout(
            lambda: _build_crawler().fetch_metadata(book_id),
            PARSE_TIMEOUT_SECONDS,
        )
    except UpstreamTimeout:
        return {"success": False, "error": "获取书籍信息超时，请稍后重试"}
    except CrawlerError as exc:
        return {"success": False, "error": str(exc).strip() or "获取书籍信息失败，请稍后重试"}
    except Exception as exc:
        logger.exception("获取书籍信息异常(book_id=%s)", book_id)
        return {"success": False, "error": _friendly_error(exc, "获取书籍信息失败，请稍后重试")}

    if not isinstance(metadata, dict):
        return {"success": False, "error": "书籍信息格式异常，请稍后重试"}

    metadata = dict(metadata)
    metadata["book_id"] = metadata.get("book_id") or book_id
    _fill_metadata_defaults(metadata)
    return {"success": True, "data": metadata}


# ------------------------------- 下载进度 SSE -------------------------------
@app.get("/api/download/progress")
async def api_download_progress(
    request: Request,
    book_id: str = Query(default="", description="书籍 ID"),
) -> Any:
    """SSE 进度流。同一 book_id 的首个订阅者会启动唯一一个后台下载线程。"""
    book_id = str(book_id or "").strip()
    if not book_id:
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": "缺少 book_id 参数"},
        )
    if not BOOK_ID_RE.match(book_id):
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": "book_id 格式不正确"},
        )

    try:
        _ensure_job(book_id)
    except Exception as exc:  # pragma: no cover - 极端兜底
        logger.exception("创建下载任务失败(book_id=%s)", book_id)
        return JSONResponse(
            status_code=200,
            content={"success": False, "error": _friendly_error(exc, "创建下载任务失败")},
        )

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(
        _sse_stream(request, book_id),
        media_type="text/event-stream",
        headers=headers,
    )


async def _sse_stream(request: Request, book_id: str):
    """事件序列: start -> downloading* -> done/error，结束即关闭连接。

    每个订阅者按事件日志索引逐条推送，保证每次 progress_callback 都能送达，
    不会因为 0.4s 轮询间隔而合并掉快速章节的进度。
    """
    try:
        with JOBS_LOCK:
            job = JOBS.get(book_id)
            snapshot = _public_job_payload(job) if job is not None else None

        if snapshot is None:
            yield _sse_pack(_error_payload(book_id, "下载任务不存在"))
            return

        # 任务已结束：立刻回放最终状态后关闭
        if snapshot["status"] in TERMINAL_STATUSES:
            yield _sse_pack(snapshot)
            return

        # 保证前端一定先收到 start 事件
        start_payload = _public_job_payload(_new_job(book_id))
        start_payload["status"] = "start"
        start_payload["message"] = "开始下载"
        yield _sse_pack(start_payload)

        next_index = 0
        last_emit = time.monotonic()

        while True:
            if await request.is_disconnected():
                return

            with JOBS_LOCK:
                job = JOBS.get(book_id)
                if job is None:
                    yield _sse_pack(_error_payload(book_id, "下载任务已丢失，请重试"))
                    return
                events = job.get("events")
                if not isinstance(events, list):
                    events = []
                pending = [dict(item) for item in events[next_index:]]
                next_index = len(events)
                latest = _public_job_payload(job)

            if pending:
                for item in pending:
                    yield _sse_pack(item)
                last_emit = time.monotonic()
                if latest["status"] in TERMINAL_STATUSES:
                    return
            else:
                now = time.monotonic()
                if now - last_emit >= SSE_HEARTBEAT_SECONDS:
                    last_emit = now
                    yield ": ping\n\n"

            await asyncio.sleep(SSE_POLL_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # 流内异常不能让整个服务崩溃
        logger.exception("SSE 推送异常(book_id=%s)", book_id)
        try:
            yield _sse_pack(_error_payload(book_id, _friendly_error(exc, "进度推送异常，请重试")))
        except Exception:
            pass


# ------------------------------- 文件下载 -------------------------------
def _file_not_found() -> JSONResponse:
    return JSONResponse(status_code=404, content={"success": False, "error": "文件不存在"})


@app.get("/api/download/file/{filename}")
def api_download_file(filename: str) -> Any:
    """安全下载 downloads/ 下的 .txt 文件（只允许纯文件名，禁止路径穿越）。"""
    raw = str(filename or "").strip()
    safe_name = Path(raw).name.strip()
    if not safe_name or safe_name != raw or not safe_name.lower().endswith(".txt"):
        return _file_not_found()

    try:
        download_root = DOWNLOAD_DIR.resolve()
        file_path = (download_root / safe_name).resolve()
        if file_path.parent != download_root or not file_path.is_file():
            return _file_not_found()
    except OSError:
        return _file_not_found()

    return FileResponse(
        str(file_path),
        media_type="text/plain; charset=utf-8",
        filename=safe_name,
    )


# ------------------------------- 页面 -------------------------------
@app.get("/")
def index_page() -> Any:
    if INDEX_FILE.is_file():
        return FileResponse(str(INDEX_FILE), media_type="text/html; charset=utf-8")
    return HTMLResponse(status_code=200, content=_MISSING_INDEX_HTML)


@app.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)


# ------------------------------- 全局异常处理 -------------------------------
@app.exception_handler(RequestValidationError)
async def _on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # 只有请求体/参数本身格式错误才返回 400；业务错误走 200 + success=false
    return JSONResponse(
        status_code=400,
        content={"success": False, "error": "请求参数格式错误，请检查提交的数据"},
    )


@app.exception_handler(Exception)
async def _on_unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("未处理的异常: %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"success": False, "error": "服务器内部错误，请稍后重试"},
    )


# ---------------------------------------------------------------------------
# 启动入口
# ---------------------------------------------------------------------------
def _print_banner(url: str) -> None:
    lines = [
        "=" * 62,
        f"  番茄小说一键下载器  v{VERSION}",
        "-" * 62,
        f"  服务地址 : {url}",
        f"  下载目录 : {DOWNLOAD_DIR}",
        f"  核心模块 : {'已加载' if CORE_READY else '加载失败（请检查 core/ 与依赖）'}",
        "  停止服务 : 按 Ctrl + C",
        "-" * 62,
        "  浏览器将自动打开；若未打开，请手动访问上面的地址。",
        "=" * 62,
    ]
    print("\n".join(lines), flush=True)
    print(
        "  签名提示 : 接口签名依赖 APP 版本，番茄改版可能失效；"
        f"失效请到 {REPO_ISSUES_URL} 提交 issue",
        flush=True,
    )


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url, new=2)
    except Exception:  # 浏览器打不开不影响服务
        pass


def main() -> None:
    # 统一输出编码，避免中文/符号在重定向或 GBK 控制台里报 UnicodeEncodeError
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

    port = _env_int("PORT", DEFAULT_PORT)
    if not (1 <= port <= 65535):
        port = DEFAULT_PORT
    url = f"http://{HOST}:{port}"

    _print_banner(url)
    if not CORE_READY:
        print("[警告] 核心模块未加载，解析/下载接口将返回中文错误提示。\n", file=sys.stderr, flush=True)

    if os.environ.get("FANQIE_NO_BROWSER", "").strip() != "1":
        timer = threading.Timer(1.2, _open_browser, args=(url,))
        timer.daemon = True
        timer.start()

    exit_code = 0
    try:
        uvicorn.run(
            app,
            host=HOST,
            port=port,
            log_level="info",
            access_log=False,
        )
    except KeyboardInterrupt:
        pass
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else 1
        if exit_code != 0:
            print(
                f"\n[错误] 服务启动失败，端口 {port} 可能已被占用。\n"
                f"       可设置环境变量 PORT 更换端口后重试，例如: set PORT=8080",
                file=sys.stderr,
                flush=True,
            )
    except OSError as exc:
        # uvicorn 在端口被占用时抛 OSError，这里转成友好的中文提示
        exit_code = 1
        print(
            f"\n[错误] 无法监听端口 {port}：{exc}\n"
            f"       端口可能已被占用，可设置环境变量 PORT 更换端口后重试，"
            f"例如: set PORT=8080",
            file=sys.stderr,
            flush=True,
        )
    finally:
        print("\n服务已停止，感谢使用。", flush=True)

    if exit_code:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
