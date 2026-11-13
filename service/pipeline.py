"""分析流水线：预览 -> 指标 -> 评分 ->（可选）标签 -> 入库。"""

from __future__ import annotations

import threading
from pathlib import Path

from . import clip_tagger, exif_tool, library, metrics, preview, scoring

VERSION = "0.1.0"

_gpu_lock = threading.Lock()


def _compact(metrics_result: dict) -> dict:
    return {
        "focus_norm": round(float(metrics_result["focus_norm"]), 4),
        "slope": round(float(metrics_result["slope"]), 4),
        "slope_primary": round(float(metrics_result["slope_primary"]), 4),
        "slope_half": round(float(metrics_result["slope_half"]), 4),
        "slope_p50": round(float(metrics_result["slope_p50"]), 4),
        "slope_p90": round(float(metrics_result.get("slope_p90", 0.0)), 4),
        "slope_all_p50": round(float(metrics_result["slope_all_p50"]), 4),
        "texture_ratio": round(float(metrics_result["texture_ratio"]), 4),
        "directional_ratio": round(float(metrics_result["directional_ratio"]), 4),
        "motion_risk": round(float(metrics_result["motion_risk"]), 4),
        "edge_angle": round(float(metrics_result["edge_angle"]), 1),
        "exposure": {key: round(float(value), 4) for key, value in metrics_result["exposure"].items()},
        "noise": {"sigma": round(float(metrics_result["noise"]["sigma"]), 3)},
        "subject": {
            "share": round(float(metrics_result["subject"]["share"]), 4),
            "p50": round(float(metrics_result["subject"].get("p50", 0.0)), 4),
            "box": [round(float(value), 4) for value in metrics_result["subject"]["box"]],
        },
        "size": metrics_result["size"],
    }


def _iso(meta: dict) -> float | None:
    try:
        value = float(meta.get("ISO"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def analyze(items: list[dict], want_tags: bool = True, force: bool = False) -> dict:
    """items: [{id, path}]；返回逐项结果与整体状态。"""
    store = library.get_library()
    tagger = clip_tagger.get_tagger() if want_tags else None
    paths = [str(item.get("path") or "") for item in items]
    metas = exif_tool.read([path for path in paths if path])

    results: list[dict] = []
    for item in items:
        entry_id = item.get("id")
        raw_path = str(item.get("path") or "")
        source = Path(raw_path)
        if not raw_path or not source.is_file():
            results.append({"id": entry_id, "ok": False, "error": "找不到文件"})
            continue
        try:
            stat = source.stat()
        except OSError as error:
            results.append({"id": entry_id, "ok": False, "error": str(error)})
            continue

        meta = exif_tool.lookup(metas, raw_path)
        cached = None if force else store.get(raw_path, stat.st_size, stat.st_mtime)
        cache_ok = cached and (
            not want_tags
            or (cached.get("tags") is not None
                and cached.get("tags_version") == clip_tagger.LABELS_VERSION)
        )
        if cache_ok:
            results.append({"id": entry_id, "ok": True, "cached": True, **cached})
            continue

        try:
            loaded = preview.load_preview(source)
            luma = metrics.to_luma(loaded["image"])
            box = tuple(meta["focus_box"]) if meta.get("focus_box") else None
            measured = metrics.analyze_luma(luma, box)
            scored = scoring.score_photo(measured, iso=_iso(meta))
        except (preview.PreviewError, ValueError, OSError) as error:
            results.append({"id": entry_id, "ok": False, "error": str(error)})
            continue

        tags: list[dict] | None = None
        if tagger is not None:
            with _gpu_lock:
                tags = tagger.tag(loaded["image"])

        payload = {
            "metrics": _compact(measured),
            "score": scored,
            "tags": tags,
            "tags_version": clip_tagger.LABELS_VERSION if tags is not None else None,
            "exif": {
                key: meta[key]
                for key in ("Model", "LensModel", "ISO", "FNumber", "ExposureTime",
                            "FocalLength", "DateTimeOriginal", "AFAreaMode", "af_kind")
                if meta.get(key) is not None
            },
            "preview": {
                "width": loaded["source_width"],
                "height": loaded["source_height"],
                "embedded": loaded["embedded"],
            },
        }
        store.put(raw_path, stat.st_size, stat.st_mtime, payload)
        results.append({"id": entry_id, "ok": True, "cached": False, **payload})

    return {
        "ok": True,
        "version": VERSION,
        "results": results,
        "tags": {
            "available": bool(tagger and tagger.ok),
            "reason": tagger.error if tagger else "",
        },
        "library": store.stats(),
    }


def status() -> dict:
    return {
        "ok": True,
        "version": VERSION,
        "exiftool": exif_tool.available(),
        "clip": {**clip_tagger.status(), "labels_version": clip_tagger.LABELS_VERSION},
        "library": library.get_library().stats(),
        "weights": scoring.normalize_weights(scoring.DEFAULT_WEIGHTS),
    }
