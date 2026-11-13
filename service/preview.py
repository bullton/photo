"""从 RAW 容器里取相机内嵌的 JPEG 预览。

思路与网页版一致：在原始字节里找 JPEG 的 SOI/EOI 对，挑面积最大、体积合理
的那一张。只解压预览，不做 RAW 显影，因此不需要任何 RAW 解码器。
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageOps

PREVIEW_LONG_EDGE = 1600
FULL_PREVIEW_MAX = 6 * 1024 * 1024
_RAW_EXT = {
    ".nef", ".nrw", ".cr2", ".cr3", ".arw", ".srf", ".sr2", ".dng", ".raf",
    ".orf", ".rw2", ".pef", ".srw", ".3fr", ".erf", ".mef", ".mos", ".mrw", ".x3f",
}


class PreviewError(RuntimeError):
    pass


def is_raw(path: str | Path) -> bool:
    return Path(path).suffix.lower() in _RAW_EXT


def _jpeg_dims(data: bytes, start: int, end: int) -> tuple[int, int] | None:
    i = start + 2
    while i + 3 < end:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker == 0xD9:
            return None
        if i + 3 >= end:
            return None
        length = (data[i + 2] << 8) | data[i + 3]
        if length < 2:
            return None
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if i + 8 < end:
                height = (data[i + 5] << 8) | data[i + 6]
                width = (data[i + 7] << 8) | data[i + 8]
                return width, height
            return None
        i += 2 + length
    return None


def find_jpeg_candidates(data: bytes) -> list[dict]:
    """返回按面积降序排列的预览候选。"""
    found: list[dict] = []
    n = len(data)
    i = 0
    while i + 3 < n:
        if data[i] != 0xFF or data[i + 1] != 0xD8:
            i += 1
            continue
        limit = min(n, i + 12 * 1024 * 1024)
        end = data.find(b"\xff\xd9", i + 2, limit)
        if end < 0:
            i += 1
            continue
        end += 2
        dims = _jpeg_dims(data, i, end)
        if dims:
            width, height = dims
            span = end - i
            area = width * height
            plausible = (
                320 <= width <= 12000
                and 240 <= height <= 12000
                and span >= 8192
                and span >= area * 0.02
            )
            if plausible:
                found.append({"start": i, "end": end, "area": area, "width": width, "height": height})
        i = end
    found.sort(key=lambda item: item["area"], reverse=True)
    return found


def load_preview(path: str | Path, long_edge: int = PREVIEW_LONG_EDGE) -> dict:
    """读取预览并归一化长边，返回图像与来源信息。"""
    source = Path(path)
    raw = source.read_bytes()
    errors: list[str] = []

    candidates = find_jpeg_candidates(raw) if is_raw(source) else []
    buffers: list[bytes] = []
    for candidate in candidates:
        buffers.append(raw[candidate["start"]:candidate["end"]])
    if not is_raw(source):
        buffers.append(raw)

    for blob in buffers:
        try:
            image = Image.open(io.BytesIO(blob))
            image.load()
            image = ImageOps.exif_transpose(image)
            width, height = image.size
            if width < 64 or height < 64:
                continue
            image = image.convert("RGB")
            scale = long_edge / float(max(width, height))
            resample = Image.LANCZOS
            if scale < 1.0:
                image = image.resize(
                    (max(1, round(width * scale)), max(1, round(height * scale))),
                    resample,
                )
            return {
                "image": image,
                "source_width": width,
                "source_height": height,
                "preview_bytes": len(blob),
                "embedded": bool(candidates),
            }
        except (OSError, ValueError) as error:  # 解不开就试下一个候选
            errors.append(str(error))
            continue

    if is_raw(source):
        raise PreviewError("此 RAW 未找到可用的内嵌预览")
    raise PreviewError("无法解码图片：" + (errors[-1] if errors else source.name))


def extract_full_preview(path: str | Path) -> bytes | None:
    """导出 1:1 查看用的全分辨率预览（体积过大时返回 None）。"""
    source = Path(path)
    if not is_raw(source):
        return None
    raw = source.read_bytes()
    for candidate in find_jpeg_candidates(raw):
        span = candidate["end"] - candidate["start"]
        if span <= FULL_PREVIEW_MAX:
            return raw[candidate["start"]:candidate["end"]]
    return None
