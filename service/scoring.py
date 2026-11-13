"""把原始指标折算成 0–100 分项与综合分。

综合分刻意做成可解释的加权和：每张照片都能在界面上看到分项和权重，
用户改权重就能实时重排，而不是接受一个来源不明的数字。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_WEIGHTS = {
    "focus": 45,
    "motion": 15,
    "exposure": 15,
    "noise": 10,
    "subject": 15,
}

VERDICT_SHARP = 50.0
VERDICT_BORDER = 30.0

LOW_DETAIL = 0.15  # 纹理占比低于此值时不下“虚焦”结论，只标待定


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def normalize_weights(weights: dict | None) -> dict:
    base = dict(DEFAULT_WEIGHTS)
    if isinstance(weights, dict):
        for key in base:
            try:
                value = float(weights.get(key, base[key]))
            except (TypeError, ValueError):
                continue
            if value >= 0:
                base[key] = value
    total = sum(base.values()) or 1.0
    return {key: value / total for key, value in base.items()}


def load_config() -> dict:
    """读取 config/scoring.json（可选），用于分权重与阈值。"""
    root = Path(__file__).resolve().parent.parent
    path = Path(os.environ.get("PHOTOPLUS_SCORING", root / "config" / "scoring.json"))
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def exposure_score(exposure: dict) -> float:
    highlight = float(exposure.get("highlight_clip", 0.0))
    shadow = float(exposure.get("shadow_clip", 0.0))
    mean = float(exposure.get("mean", 128.0))
    score = 100.0
    if highlight > 0.02:
        score -= min(45.0, (highlight - 0.02) * 420.0)
    # 夜景 / 星空本来就大面积暗部，只有整体偏暗时才算欠曝。
    if shadow > 0.45 and mean < 55.0:
        score -= min(30.0, (shadow - 0.45) * 55.0)
    if mean < 12.0:
        score -= 15.0
    return _clamp(score)


def noise_score(noise: dict, iso: float | None = None) -> float:
    sigma = float(noise.get("sigma", 0.0))
    score = 100.0 - max(0.0, sigma - 2.5) * 9.0
    if iso and iso >= 3200:
        score -= min(15.0, (iso - 3200) / 800.0)
    return _clamp(score)


def subject_score(subject: dict) -> float:
    share = float(subject.get("share", 0.0))
    return _clamp(share * 170.0)


def focus_score(metrics: dict) -> float:
    return _clamp(float(metrics.get("focus_norm", 0.0)) * 100.0)


def motion_score(metrics: dict) -> float:
    return _clamp(100.0 * (1.0 - float(metrics.get("motion_risk", 0.0))))


def verdict_of(focus: float, texture_ratio: float) -> str:
    # 主体本身细节少（剪影、雾天、纯色天空占多数）时无法判断对焦，
    # 宁可标“待定”也不要误杀。
    if texture_ratio < LOW_DETAIL and focus < VERDICT_SHARP * 1.8:
        return "border"
    if focus >= VERDICT_SHARP:
        return "sharp"
    if focus >= VERDICT_BORDER:
        return "border"
    return "blur"


def star_for(composite: float, verdict: str) -> int:
    """星级先服从对焦判定，再按综合分细分。

    这样虚焦帧不会因为曝光好、主体大而拿到 3 星以上；而待定帧最多 3 星，
    需要人眼确认是否留用。
    """
    if verdict == "blur":
        return 1 if composite >= 55 else 0
    if verdict == "border":
        if composite >= 62:
            return 3
        return 2 if composite >= 48 else 1
    if composite >= 90:
        return 5
    if composite >= 82:
        return 4
    if composite >= 72:
        return 3
    return 2


NIGHT_CLASSES = {"astrophotography", "night_scene"}
NIGHT_STAR_MIN = 2


def looks_like_night(metrics: dict) -> bool:
    """暗场照片的保底判断：不依赖 CLIP 也能识别大面积暗部 + 极低高光。"""
    exposure = metrics.get("exposure", {})
    mean = float(exposure.get("mean", 128.0))
    highlight = float(exposure.get("highlight_clip", 0.0))
    shadow = float(exposure.get("shadow_clip", 0.0))
    return mean <= 45.0 and highlight <= 0.01 and shadow >= 0.20


def score_photo(
    metrics: dict,
    weights: dict | None = None,
    iso: float | None = None,
    scenes: set[str] | tuple[str, ...] | None = None,
) -> dict:
    """返回分项分、综合分、判定与星级建议。"""
    config = load_config()
    weight_map = normalize_weights(weights or config.get("weights"))

    parts = {
        "focus": focus_score(metrics),
        "motion": motion_score(metrics),
        "exposure": exposure_score(metrics.get("exposure", {})),
        "noise": noise_score(metrics.get("noise", {}), iso),
        "subject": subject_score(metrics.get("subject", {})),
    }
    composite = sum(parts[key] * weight_map[key] for key in weight_map)
    verdict = verdict_of(parts["focus"], float(metrics.get("texture_ratio", 0.0)))
    star = star_for(composite, verdict)
    scene_keys = set(scenes or ())
    night = bool(scene_keys & NIGHT_CLASSES) or looks_like_night(metrics)
    if night:
        # 宁可交给人工复核，也不要把用户最在意的星空/夜景建议成废片。
        if verdict == "blur":
            verdict = "border"
        star = max(star, NIGHT_STAR_MIN)
    return {
        "parts": {key: round(value, 2) for key, value in parts.items()},
        "weights": {key: round(value, 4) for key, value in weight_map.items()},
        "composite": round(composite, 2),
        "verdict": verdict,
        "star": star,
        "night": night,
    }
