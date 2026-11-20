"""可选：用本机 ExifTool 补齐 EXIF / 对焦提示。

ExifTool 不存在时所有调用返回空结果，不影响评分主链路。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

FIELDS = (
    "DateTimeOriginal",
    "SubSecTimeOriginal",
    "OffsetTimeOriginal",
    "Model",
    "LensModel",
    "ISO",
    "FNumber",
    "ExposureTime",
    "FocalLength",
    "ScaleFactor35efl",
    "AFAreaMode",
    "AFPointsUsed",
    "FocusLocation",
    "AFAreaXPosition",
    "AFAreaYPosition",
    "AFAreaWidth",
    "AFAreaHeight",
    "AFImageWidth",
    "AFImageHeight",
    "ImageWidth",
    "ImageHeight",
)


def which() -> str | None:
    for name in ("exiftool", "exiftool.exe", "ExifTool.exe"):
        found = shutil.which(name)
        if found:
            return found
    override = os.environ.get("PHOTOPLUS_EXIFTOOL")
    if override and Path(override).is_file():
        return override
    # 便携版常见安装位置，避免要求用户改 PATH。
    local = os.environ.get("LOCALAPPDATA") or ""
    for candidate in (
        Path(local) / "Programs" / "ExifTool" / "ExifTool.exe" if local else None,
        Path(local) / "Programs" / "exiftool" / "exiftool.exe" if local else None,
        Path(r"C:\Program Files\ExifTool\exiftool.exe"),
        Path("/usr/local/bin/exiftool"),
        Path("/opt/homebrew/bin/exiftool"),
    ):
        if candidate and candidate.is_file():
            return str(candidate)
    return None


def available() -> bool:
    return which() is not None


def mode_label(value) -> str:
    text = str(value or "").strip().lower()
    if not text or text.isdigit():
        # 用 -n 读取时 Nikon 的 AFAreaMode 是数字枚举，没有可读含义。
        return ""
    if "area" in text or "wide" in text or "auto" in text or "track" in text:
        return "area"
    if "single" in text or "point" in text:
        return "single"
    return text


def _normalize_box(meta: dict) -> list[float] | None:
    """把对焦区域转换成归一化框。

    优先用 FocusLocation（"x y w h"，左下角+尺寸）；Nikon Z 机身只暴露
    AFAreaXPosition / AFAreaYPosition（对焦框中心）与宽高，作为回退。
    """
    try:
        width = float(meta.get("ImageWidth") or 0)
        height = float(meta.get("ImageHeight") or 0)
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None

    cx = cy = hw = hh = 0.0
    raw = meta.get("FocusLocation")
    if raw:
        parts = str(raw).replace(",", " ").split()
        if len(parts) < 4:
            return None
        try:
            x, y, w, h = (float(item) for item in parts[:4])
        except (TypeError, ValueError):
            return None
        cx = (x + w / 2.0) / width
        cy = (y + h / 2.0) / height
        hw = max(0.02, (w / width) / 2.0 * 1.35)
        hh = max(0.02, (h / height) / 2.0 * 1.35)
    else:
        try:
            px = float(meta["AFAreaXPosition"])
            py = float(meta["AFAreaYPosition"])
            aw = float(meta.get("AFAreaWidth") or 0)
            ah = float(meta.get("AFAreaHeight") or 0)
        except (KeyError, TypeError, ValueError):
            return None
        if aw <= 0 or ah <= 0:
            return None
        af_width = float(meta.get("AFImageWidth") or 0) or width
        af_height = float(meta.get("AFImageHeight") or 0) or height
        area_ratio = (aw * ah) / (af_width * af_height)
        cx = px / af_width
        cy = py / af_height
        # 单点 AF 只标位置，按客户端同样的规则扩成 30% 的主体区；
        # 区域/宽区域 AF 本身已经框住主体，只扩 15%。
        if area_ratio >= 0.008:
            hw = max(0.02, (aw / width) / 2.0 * 1.15)
            hh = max(0.02, (ah / height) / 2.0 * 1.15)
        else:
            hw = max(0.15, (aw / width) / 2.0 * 1.5)
            hh = max(0.15, (ah / height) / 2.0 * 1.5)

    if not (0.0 <= cx <= 1.0 and 0.0 <= cy <= 1.0):
        return None
    return [
        max(0.0, cx - hw),
        max(0.0, cy - hh),
        min(1.0, cx + hw),
        min(1.0, cy + hh),
    ]


def read(paths: list[str], timeout: float = 90.0) -> dict[str, dict]:
    """一次调用读取多张，返回 {绝对路径: 元数据}。"""
    binary = which()
    if not binary or not paths:
        return {}
    out: dict[str, dict] = {}
    # Windows caps a command line at ~32k characters; a whole 400+ image folder
    # in one call would silently drop EXIF, so read in small batches.
    chunk_size = 50
    for start in range(0, len(paths), chunk_size):
        chunk = paths[start:start + chunk_size]
        _read_chunk(binary, chunk, timeout, out)
    return out


def _read_chunk(binary: str, paths: list[str], timeout: float, out: dict[str, dict]) -> None:
    command = [binary, "-j", "-n", "-fast", "-q", "-q"]
    command += [f"-{name}" for name in FIELDS]
    command += [str(Path(path)) for path in paths]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return
    if completed.returncode not in (0, 1):
        return
    try:
        rows = json.loads(completed.stdout.decode("utf-8", errors="replace") or "[]")
    except ValueError:
        return

    wanted = [str(Path(path)) for path in paths]
    # ExifTool on Windows prints SourceFile in the console code page, so Chinese
    # paths come back mojibake and cannot be matched by string. -json keeps one
    # row per input in the same order, so pair them positionally instead.
    for index, row in enumerate(rows):
        source = wanted[index] if index < len(wanted) else row.get("SourceFile")
        if not source:
            continue
        entry = {key: row[key] for key in FIELDS if key in row}
        box = _normalize_box(row)
        if box:
            entry["focus_box"] = box
        entry["af_kind"] = mode_label(row.get("AFAreaMode"))
        out[str(Path(source).resolve()).lower()] = entry
        out[str(Path(source)).lower()] = entry


def lookup(meta: dict[str, dict], path: str) -> dict:
    if not meta:
        return {}
    key = str(Path(path).resolve()).lower()
    return meta.get(key) or meta.get(str(Path(path)).lower()) or {}
