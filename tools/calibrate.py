"""对焦阈值标定工具（可选，纯 CPU）。

用法：
    python tools/calibrate.py "D:\\Photos\\2026-07-04"
    python tools/calibrate.py "D:\\Photos" --limit 60
    python tools/calibrate.py "D:\\Photos" --blur

输出每张图的 slope（P75/P90/混合）与 focus_norm，并给出分位数建议。
加 --blur 会额外对每张图做 1/2/3px 高斯模糊，验证“越糊分越低”。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from service import metrics, scoring  # noqa: E402

IMAGE_EXT = {
    ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp",
    ".nef", ".nrw", ".cr2", ".cr3", ".arw", ".dng", ".raf", ".orf", ".rw2", ".pef",
}


def collect(folder: Path, limit: int) -> list[Path]:
    files = sorted(
        path for path in folder.rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_EXT
    )
    return files[:limit] if limit else files


def load(path: Path) -> Image.Image:
    from service import preview

    return preview.load_preview(path)["image"]


def slope_of(image: Image.Image) -> tuple[float, float, float]:
    result = metrics.analyze_luma(metrics.to_luma(image))
    return result["slope_p75"], result["slope_p90"], result["slope"]


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--blur", action="store_true", help="额外测 1/2/3px 模糊")
    args = parser.parse_args()

    files = collect(args.folder, args.limit)
    if not files:
        print("没有找到图片")
        return 1

    rows = []
    sharp_values = []
    blur_values = []
    print(f"{'file':<26}{'P75':>7}{'P90':>7}{'blend':>7}{'focus':>7}  verdict")
    for path in files:
        try:
            image = load(path)
        except Exception as error:  # noqa: BLE001
            print(f"{path.name:<26} 读取失败：{error.__class__.__name__}")
            continue
        p75, p90, blend = slope_of(image)
        measured = metrics.analyze_luma(metrics.to_luma(image))
        score = scoring.score_photo(measured)
        rows.append(blend)
        sharp_values.append(blend)
        print(f"{path.name[:25]:<26}{p75:7.3f}{p90:7.3f}{blend:7.3f}"
              f"{score['parts']['focus']:7.0f}  {score['verdict']}")

        if args.blur:
            parts = []
            for radius in (1, 2, 3):
                blurred = image.filter(ImageFilter.GaussianBlur(radius))
                _, _, value = slope_of(blurred)
                if radius == 2:
                    blur_values.append(value)
                parts.append(f"r{radius}={value:.3f}")
            print(f"{'  blur':<26}{'':>21}{'  '.join(parts)}")

    if rows:
        values = np.array(rows)
        print("\n分布（这批原图）")
        for label, value in (("p10", 10), ("p50", 50), ("p90", 90)):
            print(f"  {label}: {np.percentile(values, value):.3f}")
        if blur_values:
            sharp_median = float(np.median(sharp_values))
            blur_median = float(np.median(blur_values))
            print(f"\n分离度：原图 {sharp_median:.3f} / 2px 模糊 {blur_median:.3f} "
                  f"（比值 {sharp_median / max(blur_median, 1e-6):.2f}×）")
            print("建议常量（以 2px 模糊为负样本）：")
            print(f"  SLOPE_LO ≈ {blur_median * 0.8:.3f}   # 当前 {metrics.SLOPE_LO}")
            print(f"  SLOPE_HI ≈ {sharp_median:.3f}   # 当前 {metrics.SLOPE_HI}")
            print("比值低于 2.0× 说明这批片子的清晰度差异本来就小，不要硬调阈值。")
        print("注：最终阈值应以你对“留用 / 弃用”的人工判断为准。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
