import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import upscale


@unittest.skipUnless(upscale.executable("ffmpeg") and upscale.executable("ffprobe"),
                     "FFmpeg and FFprobe must be installed for real-video integration tests")
class VideoTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.data_patch = patch("upscale.DATA", self.root / "data")
        self.output_patch = patch("upscale.OUTPUTS", self.root / "output")
        self.data_patch.start()
        self.output_patch.start()

    def tearDown(self):
        self.data_patch.stop()
        self.output_patch.stop()
        self.directory.cleanup()

    def make_video(self, extension="mp4", audio=True):
        path = self.root / f"sample.{extension}"
        args = [upscale.executable("ffmpeg"), "-v", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=96x64:rate=25"]
        if audio:
            args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
        args += ["-t", "1", "-c:v", "mpeg4" if extension == "avi" else "libx264"]
        if audio:
            args += ["-c:a", "pcm_s16le" if extension == "avi" else "aac"]
        subprocess.run(args + [str(path)], check=True, capture_output=True)
        return path

    def test_avi_and_mp4_preview_and_export(self):
        for extension in ("avi", "mp4"):
            with self.subTest(extension=extension):
                source = self.make_video(extension)
                source_bytes = source.read_bytes()
                info = upscale.probe(source)
                config = upscale.settings({"seconds": 1, "start": 0.2}, info)
                job = upscale.Job(info, config, True)
                job.execute()
                self.assertEqual(job.snapshot()["status"], "done", job.snapshot().get("error"))
                enhanced = upscale.probe(job.files["enhanced"])
                original = upscale.probe(job.files["original"])
                self.assertEqual((enhanced["width"], enhanced["height"]), (192, 128))
                self.assertEqual((original["width"], original["height"]), (96, 64))
                self.assertTrue(enhanced["audio"])
                self.assertLess(abs(enhanced["duration"] - 0.8), 0.12)
                self.assertFalse(list((self.root / "data").glob("*.partial.mp4")))
                job = upscale.Job(info, config, False)
                job.execute()
                self.assertEqual(job.snapshot()["status"], "done", job.snapshot().get("error"))
                self.assertTrue(job.files["enhanced"].is_file())
                self.assertEqual(source.read_bytes(), source_bytes)
                self.assertLess(abs(upscale.probe(job.files["enhanced"])["duration"] - 1), 0.12)

    def test_forced_interlacing_doubles_output_rate(self):
        info = upscale.probe(self.make_video(audio=False))
        config = upscale.settings({"deinterlace": "tff", "scale": 1}, info)
        job = upscale.Job(info, config, False)
        job.execute()
        self.assertEqual(job.snapshot()["status"], "done", job.snapshot().get("error"))
        output = upscale.probe(job.files["enhanced"])
        self.assertEqual(output["fps"], "50")
        self.assertFalse(output["audio"])
        self.assertLess(abs(output["duration"] - 1), 0.12)

    def test_cancel_before_start_does_not_publish_output(self):
        info = upscale.probe(self.make_video())
        job = upscale.Job(info, upscale.settings({}, info), False)
        job.stop()
        job.execute()
        self.assertEqual(job.snapshot()["status"], "cancelled")
        self.assertEqual(job.files, {})
        self.assertEqual(list((self.root / "output").glob("*.mp4")), [])

    def test_missing_ai_models_is_explicit_failure(self):
        info = upscale.probe(self.make_video())
        job = upscale.Job(info, upscale.settings({"backend": "ai", "scale": 4}, info), False)
        real_executable = upscale.executable
        with patch("upscale.executable", side_effect=lambda name:
                   "/fake/ai" if name == "realesrgan-ncnn-vulkan" else real_executable(name)):
            with patch.dict("os.environ", {"REALESRGAN_MODELS": str(self.root / "absent")}):
                job.execute()
        self.assertEqual(job.snapshot()["status"], "error")
        self.assertIn("REALESRGAN_MODELS", job.snapshot()["error"])
        self.assertFalse(job.files)


if __name__ == "__main__":
    unittest.main()
