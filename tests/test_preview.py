"""RAW 内嵌预览的提取逻辑（纯 CPU，不需要 torch）。"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from service import preview  # noqa: E402


def jpeg_bytes(width: int = 640, height: int = 480, seed: int = 1) -> bytes:
    rng = np.random.default_rng(seed)
    pixels = rng.integers(0, 255, (height, width, 3), dtype=np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(pixels, mode="RGB").save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


class PreviewTest(unittest.TestCase):
    def test_finds_embedded_jpeg_in_raw_container(self):
        blob = jpeg_bytes()
        container = b"\x00" * 4096 + b"II*\x00" + b"\x00" * 2048 + blob + b"\x00" * 512
        found = preview.find_jpeg_candidates(container)
        self.assertTrue(found)
        best = found[0]
        self.assertEqual((best["width"], best["height"]), (640, 480))
        self.assertGreaterEqual(best["end"] - best["start"], len(blob) - 2)

    def test_picks_largest_candidate(self):
        small = jpeg_bytes(320, 240, seed=2)
        large = jpeg_bytes(960, 640, seed=3)
        container = small + b"\x00" * 1024 + large
        found = preview.find_jpeg_candidates(container)
        self.assertGreaterEqual(len(found), 2)
        self.assertEqual(found[0]["width"], 960)

    def test_garbage_returns_empty(self):
        self.assertEqual(preview.find_jpeg_candidates(b"\xff\xd8" + b"x" * 4096), [])
        self.assertEqual(preview.find_jpeg_candidates(b""), [])

    def test_load_plain_jpeg(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "shot.jpg"
            path.write_bytes(jpeg_bytes(800, 600, seed=4))
            loaded = preview.load_preview(path)
            self.assertFalse(loaded["embedded"])
            self.assertEqual((loaded["source_width"], loaded["source_height"]), (800, 600))
            self.assertEqual(loaded["image"].size, (800, 600))

    def test_load_raises_for_broken_raw(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "broken.nef"
            path.write_bytes(b"II*\x00" + b"\x00" * 2048)
            with self.assertRaises(preview.PreviewError):
                preview.load_preview(path)


if __name__ == "__main__":
    unittest.main()
