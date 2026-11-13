#!/usr/bin/env python3
"""焦点挑选 — 本地桌面版。

在本地起一个只监听 127.0.0.1 的小服务，用系统 WebView 打开现有界面。
Python 负责原生文件夹选择、目录扫描、按需读取原图字节和写 XMP 侧车，
界面、评分和分组逻辑继续复用 index.html / focus-core.js。
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import webview

APP_TITLE = "焦点挑选"
TOKEN = secrets.token_urlsafe(18)

RAW_EXT = {
    ".nef", ".nrw", ".cr2", ".cr3", ".arw", ".srf", ".sr2", ".dng",
    ".raf", ".orf", ".rw2", ".pef", ".srw", ".3fr", ".erf", ".mef",
    ".mos", ".mrw", ".x3f",
}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif", ".bmp", ".tif", ".tiff"}
SUPPORTED_EXT = RAW_EXT | IMAGE_EXT


def resource_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


ROOT = resource_dir()


def app_data_dir() -> Path:
    path = Path.home() / "Library" / "Application Support" / "PhotoCull"
    path.mkdir(parents=True, exist_ok=True)
    return path


def media_type(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if ext == ".png":
        return "image/png"
    if ext in {".tif", ".tiff"}:
        return "image/tiff"
    if ext == ".webp":
        return "image/webp"
    if ext == ".avif":
        return "image/avif"
    if ext == ".gif":
        return "image/gif"
    if ext == ".bmp":
        return "image/bmp"
    if ext in RAW_EXT:
        return "image/x-raw"
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def file_info(path: Path) -> dict:
    stat = path.stat()
    return {
        "path": str(path),
        "name": path.name,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "type": media_type(path.name),
    }


def list_folder(folder: Path) -> list[dict]:
    files: list[dict] = []
    with os.scandir(folder) as entries:
        for entry in sorted(entries, key=lambda item: item.name):
            if not entry.is_file():
                continue
            if Path(entry.name).suffix.lower() not in SUPPORTED_EXT:
                continue
            try:
                files.append(file_info(Path(entry.path)))
            except OSError:
                continue
    return files


def build_xmp(rating: int) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="photo-cull">\n'
        ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '  <rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/" '
        f'xmp:Rating="{rating}"/>\n'
        ' </rdf:RDF>\n'
        '</x:xmpmeta>\n'
    )


RATING_ATTR = re.compile(r'(xmp:Rating\s*=\s*")([^"]*)(")')
DESCRIPTION_TAG = re.compile(r"<rdf:Description\b[^>]*>")


def merge_xmp_rating(text: str, rating: int) -> str | None:
    """只改 xmp:Rating，保留已有 XMP 里的修图参数。"""
    if "<rdf:Description" not in text:
        return None
    if RATING_ATTR.search(text):
        return RATING_ATTR.sub(lambda m: f'{m.group(1)}{rating}{m.group(3)}', text, count=1)
    match = DESCRIPTION_TAG.search(text)
    if not match:
        return None
    tag = match.group(0)
    extra = ""
    if "xmlns:xmp" not in tag:
        extra += ' xmlns:xmp="http://ns.adobe.com/xap/1.0/"'
    extra += f' xmp:Rating="{rating}"'
    if tag.rstrip().endswith("/>"):
        new_tag = tag.rstrip()[:-2].rstrip() + extra + " />"
    else:
        new_tag = tag[:-1] + extra + ">"
    return text[: match.start()] + new_tag + text[match.end():]


class Bridge:
    """暴露给页面的原生能力。"""

    def __init__(self) -> None:
        # 必须用私有属性：pywebview 会递归遍历 js_api 的公开属性来生成函数表，
        # 公开持有 Window 会一路走进 WinForms 原生对象（AccessibilityObject…），
        # 触发 maximum recursion depth exceeded，窗口启动直接卡死。
        self._win: webview.Window | None = None

    def attach(self, window: webview.Window) -> None:
        self._win = window

    def _window(self) -> webview.Window | None:
        if self._win is not None:
            return self._win
        return webview.windows[0] if webview.windows else None

    def choose_files(self) -> dict:
        window = self._window()
        if window is None:
            return {"dir": "", "files": []}
        selected = window.create_file_dialog(
            webview.FileDialog.OPEN,
            allow_multiple=True,
            file_types=(
                "照片 (*.nef;*.nrw;*.cr2;*.cr3;*.arw;*.srf;*.sr2;*.dng;*.raf;*.orf;*.rw2;"
                "*.pef;*.srw;*.3fr;*.erf;*.mef;*.mos;*.mrw;*.x3f;*.jpg;*.jpeg;*.png;"
                "*.webp;*.avif;*.gif;*.bmp;*.tif;*.tiff)",
            ),
        )
        if not selected:
            return {"dir": "", "files": []}
        paths = [Path(unquote(item)) for item in selected]
        infos = []
        for path in paths:
            try:
                if path.is_file():
                    infos.append(file_info(path))
            except OSError:
                continue
        parents = {str(path.parent) for path in paths}
        common = parents.pop() if len(parents) == 1 else ""
        return {"dir": common, "files": infos}

    def choose_folder(self) -> str:
        window = self._window()
        if window is None:
            return ""
        selected = window.create_file_dialog(webview.FileDialog.FOLDER)
        if not selected:
            return ""
        return selected[0]

    def write_xmp(self, items: list[dict]) -> dict:
        try:
            from service import xmp as xmp_service  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            xmp_service = None
        created = updated = cleared = missing = failed = 0
        results = []
        for item in items or []:
            path_value = item.get("path") or ""
            if path_value:
                source = Path(path_value)
            else:
                source = Path(item.get("dir") or "") / (item.get("name") or "")
            try:
                rating = max(0, min(5, int(item.get("rating") or 0)))
            except (TypeError, ValueError):
                rating = 0
            entry_id = item.get("id") or ""
            if not source.is_file():
                missing += 1
                results.append({"id": entry_id, "name": source.name, "rating": rating, "status": "missing"})
                continue

            status = ""
            if xmp_service is not None:
                row = xmp_service.write_sidecar(
                    source,
                    rating,
                    [str(tag) for tag in (item.get("tags") or [])],
                    item.get("label"),
                )
                status = row.get("status") or "failed"
            else:
                sidecar = source.with_suffix(".xmp")
                try:
                    if sidecar.exists():
                        text = sidecar.read_text("utf-8", errors="replace")
                        merged = merge_xmp_rating(text, rating)
                        if merged is None:
                            status = "failed"
                        else:
                            sidecar.write_text(merged, encoding="utf-8")
                            status = "updated"
                    else:
                        sidecar.write_text(build_xmp(rating), encoding="utf-8")
                        status = "created"
                except OSError:
                    status = "failed"

            if status == "created":
                created += 1
            elif status == "updated":
                updated += 1
            elif status == "failed":
                failed += 1
            else:
                missing += 1
            if status in {"created", "updated"} and rating == 0:
                cleared += 1
            results.append({"id": entry_id, "name": source.name, "rating": rating, "status": status})
        return {
            "created": created,
            "updated": updated,
            "cleared": cleared,
            "missing": missing,
            "failed": failed,
            "results": results,
        }

    def save_csv(self, text: str, filename: str) -> dict:
        window = self._window()
        if window is None:
            return {"ok": False}
        target = window.create_file_dialog(
            webview.FileDialog.SAVE,
            save_filename=filename or "焦点挑选.csv",
            file_types=("CSV (*.csv)",),
        )
        if not target:
            return {"ok": False}
        path = Path(target if isinstance(target, str) else target[0])
        if path.suffix.lower() != ".csv":
            path = path.with_suffix(".csv")
        try:
            path.write_text(text, encoding="utf-8-sig")
        except OSError as error:
            return {"ok": False, "error": str(error)}
        return {"ok": True, "path": str(path)}


ANALYZE_LOCK = threading.Lock()


def load_service():
    """延迟导入分析服务；缺依赖时返回 (None, 原因)，不影响界面启动。"""
    try:
        from service import pipeline  # noqa: PLC0415

        return pipeline, ""
    except Exception as error:  # noqa: BLE001
        return None, f"{error.__class__.__name__}: {error}"


class Handler(BaseHTTPRequestHandler):
    server_version = "PhotoCull/1.2"

    def log_message(self, *args) -> None:  # 保持终端安静
        pass

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self, query: dict) -> bool:
        return query.get("token", [""])[0] == TOKEN

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path.startswith("/api/"):
            if not self._authorized(query):
                self._json({"error": "forbidden"}, 403)
                return
            if parsed.path == "/api/list":
                folder = Path(query.get("dir", [""])[0])
                if not folder.is_dir():
                    self._json({"error": "文件夹不存在", "files": [], "dir": str(folder)}, 404)
                    return
                try:
                    files = list_folder(folder)
                except OSError as error:
                    self._json({"error": str(error), "files": [], "dir": str(folder)}, 500)
                    return
                self._json({"dir": str(folder), "files": files})
                return
            if parsed.path == "/api/file":
                self._send_file(query)
                return
            if parsed.path == "/api/ai/status":
                pipeline, reason = load_service()
                if pipeline is None:
                    self._json({"ok": False, "error": "分析服务不可用", "reason": reason})
                    return
                try:
                    self._json(pipeline.status())
                except Exception as error:  # noqa: BLE001
                    self._json({"ok": False, "error": str(error)}, 500)
                return
            self._json({"error": "not found"}, 404)
            return

        self._send_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._authorized(query):
            self._json({"error": "forbidden"}, 403)
            return
        if not parsed.path.startswith("/api/"):
            self._json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0 or length > 64 * 1024 * 1024:
            self._json({"error": "请求体无效"}, 400)
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._json({"error": "JSON 解析失败"}, 400)
            return
        if not isinstance(payload, dict):
            self._json({"error": "JSON 结构无效"}, 400)
            return

        if parsed.path == "/api/ai/analyze":
            self._analyze(payload)
            return
        if parsed.path == "/api/xmp/write":
            self._write_xmp(payload)
            return
        self._json({"error": "not found"}, 404)

    def _analyze(self, payload: dict) -> None:
        pipeline, reason = load_service()
        if pipeline is None:
            self._json({"ok": False, "error": "分析服务不可用", "reason": reason}, 503)
            return
        items = payload.get("items") or []
        if not isinstance(items, list) or not items:
            self._json({"ok": False, "error": "没有待分析的照片"}, 400)
            return
        clean = [
            {"id": str(item.get("id") or ""), "path": str(item.get("path") or "")}
            for item in items[:500]
            if isinstance(item, dict) and item.get("path")
        ]
        try:
            with ANALYZE_LOCK:
                result = pipeline.analyze(
                    clean,
                    want_tags=bool(payload.get("tags", True)),
                    force=bool(payload.get("force", False)),
                )
        except Exception as error:  # noqa: BLE001
            self._json({"ok": False, "error": str(error)}, 500)
            return
        self._json(result)

    def _write_xmp(self, payload: dict) -> None:
        try:
            from service import xmp as xmp_service  # noqa: PLC0415
        except Exception as error:  # noqa: BLE001
            self._json({"ok": False, "error": str(error)}, 503)
            return
        items = payload.get("items") or []
        if not isinstance(items, list):
            self._json({"ok": False, "error": "items 无效"}, 400)
            return
        results = []
        for item in items[:2000]:
            if not isinstance(item, dict):
                continue
            results.append(
                xmp_service.write_sidecar(
                    item.get("path") or "",
                    item.get("rating") or 0,
                    item.get("tags") or [],
                    item.get("label"),
                    backup=bool(payload.get("backup", True)),
                )
            )
        counts = {}
        for row in results:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        self._json({"ok": True, "counts": counts, "results": results})

    def _send_static(self, path: str) -> None:
        relative = unquote(path).lstrip("/") or "index.html"
        parts = Path(relative).parts
        if ".." in parts:
            self.send_error(404)
            return
        target = ROOT / relative
        if os.environ.get("PHOTOCULL_DEBUG"):
            print("STATIC", self.path, "->", target, "file", target.is_file(), flush=True)
        if not target.is_file():
            self.send_error(404)
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix in {".html", ".js", ".css"}:
            content_type += "; charset=utf-8"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, query: dict) -> None:
        raw = query.get("path", [""])[0]
        source = Path(raw)
        if not source.is_file():
            self._json({"error": "找不到原图"}, 404)
            return
        total = source.stat().st_size
        try:
            start = max(0, int(query.get("start", ["0"])[0]))
            end = int(query.get("end", [str(total)])[0])
        except (TypeError, ValueError):
            self._json({"error": "范围无效"}, 400)
            return
        end = min(total, end)
        if start > end:
            self._json({"error": "范围无效"}, 400)
            return
        length = end - start
        self.send_response(200 if (start == 0 and length == total) else 206)
        self.send_header("Content-Type", media_type(source.name))
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            with source.open("rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = handle.read(min(262144, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass


PORT_FILE = app_data_dir() / "port"
PORT_CANDIDATES = (47653, 47654, 47655, 47656, 47657, 47658, 47659, 47660)


class LocalServer(ThreadingHTTPServer):
    # Windows 上 allow_reuse_address=True 会允许两个进程绑定同一端口，
    # 后启动的实例“抢走”连接却用错 token，表现为界面一直转圈。
    allow_reuse_address = False
    daemon_threads = True


def serve(port: int | None = None) -> ThreadingHTTPServer:
    """优先使用固定端口，让 WebView 的 IndexedDB 在重启后仍属于同一来源。"""
    candidates: list[int] = []
    if port is not None:
        candidates.append(port)
    else:
        try:
            saved = int(PORT_FILE.read_text("utf-8").strip())
            if 1024 < saved < 65536:
                candidates.append(saved)
        except (OSError, ValueError):
            pass
        candidates.extend(item for item in PORT_CANDIDATES if item not in candidates)

    server = None
    for candidate in candidates:
        try:
            server = LocalServer(("127.0.0.1", candidate), Handler)
            break
        except OSError:
            continue
    if server is None:
        server = LocalServer(("127.0.0.1", 0), Handler)
    try:
        PORT_FILE.write_text(str(server.server_address[1]), encoding="utf-8")
    except OSError:
        pass
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def main() -> None:
    server = serve()
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/index.html?desktop=1&token={TOKEN}"
    bridge = Bridge()
    smoke = "--smoke" in sys.argv
    if smoke:
        print("ROOT", ROOT, "EXISTS", (ROOT / "index.html").is_file(), flush=True)
    window = webview.create_window(
        APP_TITLE,
        url,
        js_api=bridge,
        width=1280,
        height=860,
        min_size=(980, 640),
        background_color="#0d0f11",
        text_select=True,
        hidden=smoke,
    )
    bridge.attach(window)

    def smoke_check() -> None:
        import time
        time.sleep(2.5)
        try:
            info = window.evaluate_js(
                "JSON.stringify({href: location.href, title: document.title, "
                "btn: !!document.getElementById('addFolderBtn'), "
                "aiBtn: !!document.getElementById('aiRunBtn'), "
                "aiPanel: !!document.getElementById('aiPanelStat'), "
                "aiCore: typeof window.AIEnhance, "
                "aiPing: (function(){try{var x=new XMLHttpRequest();"
                "x.open('GET','/api/ai/status?token='+(new URLSearchParams(location.search).get('token')||''),false);"
                "x.send();return x.status+':'+x.responseText.slice(0,60);}catch(e){return 'ERR';}})(), "
                "text: (document.body.innerText || '').slice(0, 80)})"
            )
            print("SMOKE_INFO", info, flush=True)
        except Exception as error:  # noqa: BLE001
            print(f"SMOKE_ERR {error}", flush=True)
        window.destroy()

    webview.start(
        smoke_check if smoke else None,
        private_mode=False,
        storage_path=str(app_data_dir()),
        debug="--debug" in sys.argv,
    )


if __name__ == "__main__":
    main()
