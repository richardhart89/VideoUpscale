import unittest
from unittest.mock import patch

import upscale


def sample_info():
    return {"width": 720, "height": 576, "sar": "16/15", "fps": "25",
            "duration": 10, "field_order": "progressive", "video_index": 0}


class PipelineTests(unittest.TestCase):
    def test_cleanup_and_square_pixel_output(self):
        info = sample_info()
        config = upscale.settings({}, info)
        self.assertEqual(upscale.dimensions(info, config), (1536, 1152))
        self.assertFalse(any("bwdif" in item for item in upscale.cleanup_filters(info, config)))
        self.assertEqual(upscale.output_fps(info, config), 25)
        filters = upscale.cleanup_filters(info, config) + upscale.finish_filters(info, config)
        self.assertTrue(filters[0].startswith("hqdn3d"))
        self.assertIn("setsar=1", filters)
        self.assertTrue(filters[-2].startswith("unsharp"))

    def test_interlacing_metadata_and_overrides(self):
        info = sample_info()
        for order, parity in (("tt", "tff"), ("bt", "tff"), ("bb", "bff"), ("tb", "bff")):
            info["field_order"] = order
            config = upscale.settings({}, info)
            self.assertIn("parity=" + parity, upscale.cleanup_filters(info, config)[0])
            self.assertEqual(upscale.output_fps(info, config), 50)
        config = upscale.settings({"deinterlace": "off", "denoise": 0, "sharpen": 0}, info)
        self.assertEqual(upscale.cleanup_filters(info, config), [])
        self.assertEqual(upscale.output_fps(info, config), 25)

    def test_reject_invalid_settings(self):
        for data in ({"scale": 3}, {"scale": True}, {"scale": None}, {"denoise": "nan"},
                     {"sharpen": 8}, {"start": 10}, {"seconds": 0},
                     {"backend": "bad"}, {"deinterlace": "bad"}, {"backend": "ai", "scale": 2}):
            with self.subTest(data=data), self.assertRaises(upscale.UserError):
                upscale.settings(data, sample_info())

    def test_unknown_field_order_does_not_destroy_progressive_frames(self):
        info = dict(sample_info(), field_order="unknown")
        config = upscale.settings({}, info)
        self.assertEqual(upscale.output_fps(info, config), 25)
        config = upscale.settings({"deinterlace": "bff"}, info)
        self.assertEqual(upscale.output_fps(info, config), 50)

    def test_missing_probe_has_actionable_error(self):
        with patch("upscale.executable", return_value=None):
            with self.assertRaisesRegex(upscale.UserError, "brew install ffmpeg"):
                upscale.probe("missing.mp4")

    def test_source_validation(self):
        for value in (None, "", "/nonexistent.mp4", 42):
            with self.assertRaises(upscale.UserError):
                upscale.source_path(value)


if __name__ == "__main__":
    unittest.main()
