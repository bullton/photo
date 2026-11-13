"""纯 CPU 的指标 / 打分 / XMP 合并测试，不需要 torch 或 node。

运行：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from service import clip_tagger, metrics, scoring, xmp  # noqa: E402
from service.exif_tool import _normalize_box, mode_label  # noqa: E402


def textured_image(size: int = 512, period: float = 9.0, seed: int = 7) -> Image.Image:
    """带中频条纹与微噪声的合成图：足够接近“有细节的照片”。"""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size]
    base = 118 + 50 * np.sin(xx / period) * np.cos(yy / (period * 1.3))
    base += 6.0 * rng.standard_normal((size, size))
    return Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), mode="L")


class MetricsTest(unittest.TestCase):
    def setUp(self):
        self.sharp = textured_image()
        self.blurred = self.sharp.filter(ImageFilter.GaussianBlur(radius=4.5))

    def analyze(self, image):
        return metrics.analyze_luma(metrics.to_luma(image))

    def test_blur_lowers_focus(self):
        sharp = self.analyze(self.sharp)
        blurred = self.analyze(self.blurred)
        self.assertGreater(sharp["slope"], blurred["slope"])
        self.assertGreater(sharp["focus_norm"], blurred["focus_norm"])
        self.assertGreaterEqual(sharp["focus_norm"] - blurred["focus_norm"], 0.15)

    def test_focus_is_monotonic_in_blur(self):
        scores = [self.analyze(self.sharp.filter(ImageFilter.GaussianBlur(radius))
                               )["focus_norm"] for radius in (0, 2, 4, 8)]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_flat_image_has_no_texture(self):
        flat = Image.fromarray(np.full((256, 256), 128, dtype=np.uint8), mode="L")
        result = self.analyze(flat)
        self.assertLess(result["texture_ratio"], 0.05)
        self.assertEqual(scoring.verdict_of(scoring.focus_score(result), result["texture_ratio"]), "border")

    def test_metrics_are_serialisable(self):
        result = self.analyze(self.sharp)
        compact = {key: value for key, value in result.items()
                   if not isinstance(value, np.ndarray)}
        for key, value in compact.items():
            if isinstance(value, dict):
                for inner in value.values():
                    self.assertIsInstance(inner, (int, float, list, str))
            else:
                self.assertIsInstance(value, (int, float, list, str))

    def test_one_direction_blur_lowers_directional_ratio(self):
        def motion_blur(image, length=15):
            rows = np.asarray(image, dtype=np.float32)
            kernel = np.ones(length, dtype=np.float32) / length
            smeared = np.apply_along_axis(
                lambda row: np.convolve(row, kernel, mode="same"), 1, rows)
            return Image.fromarray(np.clip(smeared, 0, 255).astype(np.uint8), mode="L")

        sharp = self.analyze(self.sharp)
        blurred = self.analyze(motion_blur(self.sharp))
        self.assertGreater(sharp["directional_ratio"], 0.55)
        self.assertLess(blurred["directional_ratio"], 0.35)
        self.assertGreater(blurred["motion_risk"], sharp["motion_risk"])
        self.assertLess(scoring.motion_score(blurred), scoring.motion_score(sharp) - 30)


class ScoringTest(unittest.TestCase):
    def test_weights_normalise(self):
        weights = scoring.normalize_weights({"focus": 90, "motion": 0, "exposure": 0, "noise": 0, "subject": 0})
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=6)
        self.assertAlmostEqual(weights["focus"], 1.0, places=6)

    def test_verdict_thresholds(self):
        self.assertEqual(scoring.verdict_of(80, 0.5), "sharp")
        self.assertEqual(scoring.verdict_of(30, 0.5), "border")
        self.assertEqual(scoring.verdict_of(10, 0.5), "blur")
        # 低纹理不轻易判虚焦
        self.assertEqual(scoring.verdict_of(10, 0.01), "border")

    def test_stars_respect_verdict(self):
        self.assertEqual(scoring.star_for(90, "blur"), 1)
        self.assertEqual(scoring.star_for(30, "blur"), 0)
        self.assertLessEqual(scoring.star_for(100, "border"), 3)
        self.assertEqual(scoring.star_for(90, "sharp"), 5)
        self.assertGreaterEqual(scoring.star_for(60, "sharp"), 2)
        # 综合分越高，同一判定下的星级不会倒退
        scores = [scoring.star_for(value, "sharp") for value in (50, 63, 75, 87, 99)]
        self.assertEqual(scores, sorted(scores))

    def test_exposure_penalties(self):
        clipped = {"highlight_clip": 0.4, "shadow_clip": 0.0, "mean": 220.0}
        fine = {"highlight_clip": 0.0, "shadow_clip": 0.0, "mean": 128.0}
        self.assertLess(scoring.exposure_score(clipped), scoring.exposure_score(fine))
        night = {"highlight_clip": 0.0, "shadow_clip": 0.8, "mean": 20.0}
        self.assertLess(scoring.exposure_score(night), 100.0)

    def test_noise_penalty_scales_with_sigma(self):
        self.assertGreater(scoring.noise_score({"sigma": 2.0}), scoring.noise_score({"sigma": 8.0}))


class XmpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.image = self.dir / "DSC_0001.NEF"
        self.image.write_bytes(b"raw")

    def tearDown(self):
        self.tmp.cleanup()

    def sidecar(self) -> Path:
        return self.dir / "DSC_0001.xmp"

    def test_creates_sidecar_with_rating_and_tags(self):
        result = xmp.write_sidecar(self.image, 4, ["bird", "AI/wildlife"], label="Green")
        self.assertEqual(result["status"], "created")
        text = self.sidecar().read_text("utf-8")
        self.assertIn('xmp:Rating="4"', text)
        self.assertIn("AI/bird", text)
        self.assertIn("AI/wildlife", text)
        self.assertIn('xmp:Label="Green"', text)

    def test_merges_and_preserves_foreign_fields(self):
        self.sidecar().write_text(
            """<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description xmlns:xmp="http://ns.adobe.com/xap/1.0/"
      xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
      xmlns:dc="http://purl.org/dc/elements/1.1/"
      xmp:Rating="2" crs:Exposure2012="+0.35" crs:ProcessVersion="11.0">
   <dc:subject><rdf:Bag><rdf:li>鸟类</rdf:li><rdf:li>AI/food</rdf:li></rdf:Bag></dc:subject>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>""",
            encoding="utf-8",
        )
        result = xmp.write_sidecar(self.image, 5, ["bird"])
        self.assertEqual(result["status"], "updated")

        root = ET.fromstring(self.sidecar().read_text("utf-8"))
        text = ET.tostring(root, encoding="unicode")
        self.assertIn('Rating="5"', text)
        self.assertIn("Exposure2012", text)
        self.assertIn("+0.35", text)
        self.assertIn("鸟类", text)
        self.assertIn("AI/bird", text)
        self.assertNotIn("AI/food", text)
        self.assertEqual(text.count("AI/bird"), 1)
        self.assertTrue((self.dir / "DSC_0001.xmp.bak").exists())

    def test_repeat_write_does_not_duplicate_tags(self):
        xmp.write_sidecar(self.image, 3, ["bird"])
        xmp.write_sidecar(self.image, 3, ["bird"])
        text = self.sidecar().read_text("utf-8")
        self.assertEqual(text.count("AI/bird"), 1)

    def test_missing_photo_is_reported(self):
        result = xmp.write_sidecar(self.dir / "nope.NEF", 3, ["bird"])
        self.assertEqual(result["status"], "missing")
        self.assertFalse((self.dir / "nope.xmp").exists())

    def test_broken_sidecar_is_not_destroyed(self):
        broken = "<x:xmpmeta><unclosed>"
        self.sidecar().write_text(broken, encoding="utf-8")
        result = xmp.write_sidecar(self.image, 4, ["bird"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.sidecar().read_text("utf-8"), broken)


class ExifToolTest(unittest.TestCase):
    def test_focus_location_becomes_normalized_box(self):
        meta = {"FocusLocation": "2800 1800 200 200", "ImageWidth": 5600, "ImageHeight": 3728}
        box = _normalize_box(meta)
        self.assertIsNotNone(box)
        left, top, right, bottom = box
        self.assertAlmostEqual((left + right) / 2, 0.518, places=2)
        self.assertAlmostEqual((top + bottom) / 2, 0.510, places=2)
        self.assertLess(left, right)
        self.assertLess(top, bottom)

    def test_focus_location_rejects_incomplete_data(self):
        self.assertIsNone(_normalize_box({"FocusLocation": "10 20"}))
        self.assertIsNone(_normalize_box({"FocusLocation": "1 2 3 4"}))
        self.assertIsNone(_normalize_box({"FocusLocation": "99999 2 3 4", "ImageWidth": 100, "ImageHeight": 100}))

    def test_af_area_mode_labels(self):
        self.assertEqual(mode_label("Single Point"), "single")
        self.assertEqual(mode_label("Wide (L)"), "area")
        self.assertEqual(mode_label(193), "")
        self.assertEqual(mode_label(""), "")


class ClipLabelsTest(unittest.TestCase):
    def test_labels_are_complete(self):
        labels = clip_tagger.LABELS
        self.assertGreaterEqual(len(labels), 15)
        for key, value in labels.items():
            self.assertTrue(value["zh"], key)
            self.assertTrue(value["prompts"], key)
            self.assertTrue(all(isinstance(prompt, str) and prompt for prompt in value["prompts"]))

    def test_ship_categories_exist(self):
        for key in ("warship", "ship", "boat", "aircraft", "vehicle"):
            self.assertIn(key, clip_tagger.LABELS)
        self.assertIn("other", clip_tagger.LABELS)

    def test_version_bumps_with_labels(self):
        self.assertIsInstance(clip_tagger.LABELS_VERSION, int)
        self.assertGreaterEqual(clip_tagger.LABELS_VERSION, 2)


if __name__ == "__main__":
    unittest.main()
