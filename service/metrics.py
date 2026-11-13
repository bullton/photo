"""清晰度 / 曝光 / 噪声 / 运动模糊指标。

只用 numpy 计算，不依赖 OpenCV。所有指标都基于相机内嵌预览（长边归一化到
1600px），因此反映的是“相机直出 JPEG 的可辨识锐度”，不是传感器原始对焦精度。
"""

from __future__ import annotations

import math

import numpy as np

BASE_LONG_EDGE = 1600
BLOCK = 32
LOW_TEXTURE_STD = 6.0  # 0–255 亮度尺度下的分块标准差阈值

# 反差归一化斜率到 0–100 的绝对标定区间。默认值由一组鸟类 / 野生动物样片
# 标定，换机身或换机内锐化档位后应重新校准（见 docs/AI-ENHANCE.md）。
SLOPE_LO = 0.10
SLOPE_HI = 0.60

CENTER_BOX = (0.19, 0.19, 0.81, 0.81)


def to_luma(image) -> np.ndarray:
    """PIL 图像 -> float32 亮度平面（0–255）。"""
    gray = image.convert("L")
    return np.asarray(gray, dtype=np.float32)


def _integral2d(values: np.ndarray) -> np.ndarray:
    """二维前缀和，首行首列补零，便于 O(1) 求块内均值。"""
    padded = np.zeros((values.shape[0] + 1, values.shape[1] + 1), dtype=np.float64)
    padded[1:, 1:] = np.cumsum(np.cumsum(values, axis=0), axis=1)
    return padded


def _block_means(values: np.ndarray, block: int) -> np.ndarray:
    """按 block x block 切块求均值，右下不足一块的部分丢弃。"""
    h, w = values.shape
    bh, bw = h // block, w // block
    if bh < 1 or bw < 1:
        return np.array([[float(values.mean())]], dtype=np.float32)
    integral = _integral2d(values)
    ys = np.arange(bh) * block
    xs = np.arange(bw) * block
    y0 = ys[:, None]
    x0 = xs[None, :]
    y1 = y0 + block
    x1 = x0 + block
    total = (
        integral[y1, x1]
        - integral[y0, x1]
        - integral[y1, x0]
        + integral[y0, x0]
    )
    return (total / float(block * block)).astype(np.float32)


def _gradients(luma: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """中心差分梯度，边界补零。"""
    gx = np.zeros_like(luma)
    gy = np.zeros_like(luma)
    if luma.shape[1] > 2:
        gx[:, 1:-1] = (luma[:, 2:] - luma[:, :-2]) * 0.5
    if luma.shape[0] > 2:
        gy[1:-1, :] = (luma[2:, :] - luma[:-2, :]) * 0.5
    return gx, gy


def _box_blur3(luma: np.ndarray) -> np.ndarray:
    """3x3 均值模糊（边缘复制），用于估计高频残差。"""
    padded = np.pad(luma, 1, mode="edge")
    total = np.zeros_like(luma)
    for dy in (0, 1, 2):
        for dx in (0, 1, 2):
            total += padded[dy : dy + luma.shape[0], dx : dx + luma.shape[1]]
    return total / 9.0


def _box_blur(luma: np.ndarray, radius: int) -> np.ndarray:
    """k x k 均值模糊（边缘复制），用前缀和做可分离卷积。"""
    k = 2 * radius + 1
    if radius <= 0:
        return luma.astype(np.float32)
    padded = np.pad(luma.astype(np.float64), radius, mode="edge")
    rows = np.cumsum(padded, axis=0)
    rows = np.vstack((np.zeros((1, rows.shape[1])), rows))
    horizontal = rows[k : k + luma.shape[0]] - rows[: luma.shape[0]]
    cols = np.cumsum(horizontal, axis=1)
    cols = np.hstack((np.zeros((cols.shape[0], 1)), cols))
    total = cols[:, k : k + luma.shape[1]] - cols[:, : luma.shape[1]]
    return (total / float(k * k)).astype(np.float32)


def _percentile(values: np.ndarray, p: float) -> float:
    if values.size == 0:
        return 0.0
    return float(np.percentile(values, p))


def _normalized_slope(luma: np.ndarray, block: int = BLOCK) -> dict:
    """反差归一化斜率：块内梯度幅值均值 / 块内亮度标准差。

    除以局部反差是关键：高反差的虚焦树枝不会因为“更亮更花”而赢过低反差
    但实焦的主体。返回分块分布与纹理占比。
    """
    gx, gy = _gradients(luma)
    energy = gx * gx + gy * gy
    grad = np.sqrt(energy)

    std = np.sqrt(np.maximum(_block_means(luma * luma, block) -
                             _block_means(luma, block) ** 2, 0.0))
    grad_mean = _block_means(grad, block)
    slope = grad_mean / (std + 1e-3)

    textured = std >= LOW_TEXTURE_STD
    texture_ratio = float(textured.mean()) if textured.size else 0.0
    values = slope[textured] if textured.any() else slope.reshape(-1)
    flat = slope.reshape(-1)

    # 主体区纹理占比：背景虚化不会计入。
    return {
        "slope_p75": _percentile(values, 75),
        "slope_p50": _percentile(values, 50),
        "slope_max": float(values.max()) if values.size else 0.0,
        "slope_all_p50": _percentile(flat, 50),
        "texture_ratio": texture_ratio,
        "block_std_mean": float(std.mean()) if std.size else 0.0,
        "block_slope": slope,
        "block_std": std,
        "block_shape": slope.shape,
        "block": block,
    }


MOTION_RATIO_LO = 0.18  # 方向能量比低于此值视为明显拖影
MOTION_RATIO_HI = 0.55  # 高于此值视为各向细节正常


def _motion(luma: np.ndarray) -> dict:
    """运动模糊：带通细节在 4 个方向上的能量比。

    相机抖动 / 主体移动会在一个方向上抹掉细节，表现为某个方向的梯度能量
    明显偏低。这里先做 5x5 均值高通，只看中高频细节，避免被大块明暗结构
    （如天空与地面交界）误导。
    """
    band = luma - _box_blur(luma, 2)
    gx, gy = _gradients(band)
    angles = (0.0, math.pi / 4.0, math.pi / 2.0, 3.0 * math.pi / 4.0)
    energies = []
    for theta in angles:
        directional = gx * math.cos(theta) + gy * math.sin(theta)
        energies.append(float(np.mean(directional * directional)))
    largest = max(energies) if energies else 0.0
    smallest = min(energies) if energies else 0.0
    ratio = smallest / largest if largest > 1e-9 else 1.0
    risk = float(np.clip((MOTION_RATIO_HI - ratio) / (MOTION_RATIO_HI - MOTION_RATIO_LO), 0.0, 1.0))
    return {
        "directional_ratio": ratio,
        "energy_by_angle": energies,
        "edge_angle": math.degrees(angles[energies.index(largest)]) if energies else 0.0,
        "risk": risk,
    }


def _exposure(luma: np.ndarray) -> dict:
    total = float(luma.size) or 1.0
    hist, _ = np.histogram(luma, bins=32, range=(0, 256))
    prob = hist.astype(np.float64) / total
    entropy = float(-(prob[prob > 0] * np.log2(prob[prob > 0])).sum())
    highlight = float((luma >= 250).sum()) / total
    shadow = float((luma <= 4).sum()) / total
    mid = float(((luma > 40) & (luma < 215)).sum()) / total
    return {
        "mean": float(luma.mean()),
        "highlight_clip": highlight,
        "shadow_clip": shadow,
        "mid_ratio": mid,
        "entropy": entropy,
    }


def _noise(luma: np.ndarray, textured: np.ndarray, block: int = BLOCK) -> dict:
    high = luma - _box_blur3(luma)
    shape = textured.shape
    if shape[0] < 1 or shape[1] < 1:
        return {"sigma": 0.0}
    if high.shape[0] < block or high.shape[1] < block:
        return {"sigma": 0.0}
    # 把逐像素高频残差按块降到一维，取低纹理块的鲁棒标准差。
    sample = np.abs(high[: shape[0] * block, : shape[1] * block])
    sample = sample.reshape(shape[0], block, shape[1], block).mean(axis=(1, 3))
    flat = sample[~textured] if (~textured).any() else sample.reshape(-1)
    sigma = 1.4826 * float(np.median(flat)) if flat.size else 0.0
    return {"sigma": sigma}


def _subject_stats(metrics: dict, box: tuple[float, float, float, float]) -> dict:
    slope = metrics["block_slope"]
    std = metrics["block_std"]
    rows, cols = slope.shape
    x0, y0, x1, y1 = box
    c0 = max(0, min(cols - 1, int(round(x0 * cols))))
    c1 = max(c0 + 1, min(cols, int(round(x1 * cols))))
    r0 = max(0, min(rows - 1, int(round(y0 * rows))))
    r1 = max(r0 + 1, min(rows, int(round(y1 * rows))))
    inner = slope[r0:r1, c0:c1]
    inner_std = std[r0:r1, c0:c1]
    if inner.size == 0:
        return {"share": 0.0, "p50": 0.0, "box": [x0, y0, x1, y1]}
    textured = inner_std >= LOW_TEXTURE_STD
    values = inner[textured] if textured.any() else inner.reshape(-1)
    share = float(textured.mean()) if textured.size else 0.0
    return {
        "share": share,
        "p50": _percentile(values, 50),
        "p75": _percentile(values, 75),
        "box": [float(x0), float(y0), float(x1), float(y1)],
    }


def analyze_luma(luma: np.ndarray, box: tuple[float, float, float, float] | None = None) -> dict:
    """对亮度平面计算完整指标集合；box 为归一化主体区（AF 框或中心区）。"""
    if luma.ndim != 2 or luma.size < 64:
        raise ValueError("invalid luma plane")

    primary = _normalized_slope(luma)
    # 第二尺度只作为诊断信息：降采样会让“幅度衰减”和“细节丢失”混在一起，
    # 直接参与打分反而会掩盖虚焦（见 docs/AI-ENHANCE.md 的标定说明）。
    half = luma[::2, ::2]
    secondary = _normalized_slope(half)

    focus_slope = primary["slope_p75"]
    exposure = _exposure(luma)
    noise = _noise(luma, primary["block_std"] >= LOW_TEXTURE_STD)
    subject = _subject_stats(primary, box or CENTER_BOX)
    motion = _motion(luma)

    # 对数比值映射：低端更敏感（虚焦压缩在 0.1 附近），高端不会过早饱和。
    focus_norm = float(np.clip(
        math.log(max(focus_slope, 1e-6) / SLOPE_LO) / math.log(SLOPE_HI / SLOPE_LO),
        0.0, 1.0))
    # 方向能量比 + 整体清晰度：实焦画面即使主体单一方向也不该被重罚。
    motion_risk = float(np.clip(motion["risk"] * (1.0 - 0.7 * focus_norm), 0.0, 1.0))

    return {
        "slope": focus_slope,
        "slope_primary": primary["slope_p75"],
        "slope_half": secondary["slope_p75"],
        "slope_p50": primary["slope_p50"],
        "slope_all_p50": primary["slope_all_p50"],
        "focus_norm": focus_norm,
        "texture_ratio": primary["texture_ratio"],
        "directional_ratio": motion["directional_ratio"],
        "motion_risk": motion_risk,
        "edge_angle": motion["edge_angle"],
        "exposure": exposure,
        "noise": noise,
        "subject": subject,
        "block_slope": primary["block_slope"],
        "block_std": primary["block_std"],
        "size": [int(luma.shape[1]), int(luma.shape[0])],
    }
