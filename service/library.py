"""本地 SQLite 结果库：让评分跨会话保留，不必每次重新分析。"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
  path TEXT PRIMARY KEY,
  size INTEGER NOT NULL,
  mtime REAL NOT NULL,
  analyzed_at REAL NOT NULL,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_photos_size ON photos(size, mtime);
"""


def default_path() -> Path:
    override = os.environ.get("PHOTOPLUS_LIBRARY")
    if override:
        return Path(override)
    base = Path.home() / "Library" / "Application Support" / "PhotoCull"
    return base / "library.sqlite3"


class Library:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else default_path()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        except (OSError, sqlite3.Error):
            # 只读介质等情况下降级为无缓存，不影响分析。
            self._conn = None
        self._lock = threading.Lock()

    def get(self, path: str, size: int, mtime: float) -> dict | None:
        if self._conn is None:
            return None
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT size, mtime, payload FROM photos WHERE path = ?", (str(path),)
                ).fetchone()
        except sqlite3.Error:
            return None
        if not row:
            return None
        stored_size, stored_mtime, payload = row
        if int(stored_size) != int(size) or abs(float(stored_mtime) - float(mtime)) > 0.5:
            return None
        try:
            return json.loads(payload)
        except ValueError:
            return None

    def put(self, path: str, size: int, mtime: float, payload: dict) -> None:
        if self._conn is None:
            return
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO photos(path, size, mtime, analyzed_at, payload) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (str(path), int(size), float(mtime), time.time(),
                     json.dumps(payload, ensure_ascii=False)),
                )
                self._conn.commit()
        except sqlite3.Error:
            pass

    def stats(self) -> dict:
        if self._conn is None:
            return {"count": 0, "available": False}
        try:
            with self._lock:
                count = self._conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
        except sqlite3.Error:
            return {"count": 0, "available": False}
        return {"count": int(count), "available": True, "path": str(self.path)}


_library: Library | None = None
_lock = threading.Lock()


def get_library() -> Library:
    global _library
    with _lock:
        if _library is None:
            _library = Library()
        return _library
