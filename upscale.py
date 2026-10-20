#!/usr/bin/env python3
"""Local-only video restoration UI and processing worker."""

import argparse
import json
import math
import mimetypes
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import threading
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
import uuid
import webbrowser


ROOT = Path(__file__).resolve().parent
DATA = Path.home() / "Library" / "Application Support" / "VideoUpscale"
OUTPUTS = Path.home() / "Movies" / "VideoUpscale"


class UserError(Exception):
    pass


def executable(name):
    override = os.environ.get(name.upper().replace("-", "_") + "_PATH")
    if override:
        path = Path(override).expanduser()
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    found = shutil.which(name)
    if found:
        return found
    for base in ("/opt/homebrew/bin", "/usr/local/bin"):
        path = Path(base) / name
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def engines():
    return {name: executable(name) for name in
            ("ffmpeg", "ffprobe", "realesrgan-ncnn-vulkan")}


def run_capture(args):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    if result.returncode:
        raise UserError(result.stderr[-6000:] or "The video engine failed.")
    return result.stdout


def source_path(value):
    if not isinstance(value, str) or not value:
        raise UserError("Choose an AVI or MP4 video first.")
    path = Path(value).expanduser().resolve()
    if not path.is_file() or path.suffix.lower() not in (".avi", ".mp4"):
        raise UserError("Input must be an existing AVI or MP4 file.")
    return path


def probe(path):
    tool = executable("ffprobe")
    if not tool:
        raise UserError("FFprobe is missing. Install FFmpeg with: brew install ffmpeg")
    payload = json.loads(run_capture([
        tool, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)
    ]))
    video = next((s for s in payload.get("streams", [])
                  if s["codec_type"] == "video" and
                  not s.get("disposition", {}).get("attached_pic")), None)
    if not video:
        raise UserError("This file contains no usable video stream.")
    if video.get("pix_fmt", "").startswith(("yuv420p10", "yuv422p10", "yuv444p10")) or any(
            s.get("color_transfer") in ("smpte2084", "arib-std-b67")
            for s in payload.get("streams", [])):
        raise UserError("HDR / 10-bit input is not supported by this SDR-only version.")
    try:
        fps = Fraction(video.get("avg_frame_rate", "0/0"))
        duration = float(payload.get("format", {}).get("duration") or video.get("duration", 0))
        raw_sar = video.get("sample_aspect_ratio", "1:1")
        sar = Fraction((raw_sar if raw_sar not in ("N/A", "0:1", "0:0") else "1:1")
                       .replace(":", "/"))
    except (ValueError, ZeroDivisionError):
        raise UserError("Cannot determine duration, frame rate, or pixel aspect ratio.")
    if fps <= 0 or not math.isfinite(duration) or duration <= 0 or sar <= 0:
        raise UserError("The input must have a known duration and positive frame rate.")
    rotation = next((s.get("rotation", 0) for s in video.get("side_data_list", [])
                     if "rotation" in s), 0)
    if rotation:
        raise UserError("Rotated video is not supported yet. Normalize its rotation first.")
    return {
        "path": str(path), "name": path.name, "width": int(video["width"]),
        "height": int(video["height"]), "fps": str(fps), "duration": duration,
        "sar": str(sar), "field_order": video.get("field_order", "unknown"),
        "video_index": video["index"],
        "audio": any(s["codec_type"] == "audio" for s in payload.get("streams", [])),
    }


def number(data, key, default, low, high):
    value = data.get(key, default)
    if isinstance(value, bool):
        raise UserError("Invalid value for " + key)
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise UserError("Invalid value for " + key)
    if not math.isfinite(value) or not low <= value <= high:
        raise UserError(f"{key} must be between {low} and {high}.")
    return value


def settings(data, info):
    backend = data.get("backend", "ffmpeg")
    deinterlace = data.get("deinterlace", "auto")
    if backend not in ("ffmpeg", "ai"):
        raise UserError("Unknown processing engine.")
    if deinterlace not in ("auto", "off", "tff", "bff"):
        raise UserError("Unknown deinterlacing mode.")
    scale = number(data, "scale", 2, 1, 4)
    if scale not in (1, 2, 4):
        raise UserError("Scale must be 1, 2, or 4.")
    if backend == "ai" and scale != 4:
        raise UserError("The Real-ESRGAN x4plus model requires 4x scaling.")
    start = number(data, "start", 0, 0, max(0, info["duration"] - 0.01))
    return {
        "backend": backend, "deinterlace": deinterlace, "scale": int(scale),
        "denoise": number(data, "denoise", 1.5, 0, 6),
        "sharpen": number(data, "sharpen", 0.35, 0, 1.5), "start": start,
        "seconds": number(data, "seconds", 5, 1, 15),
    }


def dimensions(info, config):
    # Normalize non-square DVD / DV pixels before either upscaling engine.
    width = max(2, round(info["width"] * float(Fraction(info["sar"])) / 2) * 2)
    height = max(2, round(info["height"] / 2) * 2)
    return width * config["scale"], height * config["scale"]


def cleanup_filters(info, config):
    filters = []
    mode = config["deinterlace"]
    order = info["field_order"]
    if mode in ("tff", "bff") or (mode == "auto" and order in ("tt", "bb", "tb", "bt")):
        parity = mode if mode != "auto" else ("tff" if order in ("tt", "bt") else "bff")
        filters.append(f"bwdif=mode=send_field:parity={parity}:deint=all")
    if config["denoise"]:
        n = config["denoise"]
        filters.append(f"hqdn3d={n}:{n * 0.75}:{n * 1.5}:{n * 1.125}")
    return filters


def output_fps(info, config):
    return Fraction(info["fps"]) * (2 if any(
        f.startswith("bwdif") for f in cleanup_filters(info, config)) else 1)


def finish_filters(info, config):
    w, h = dimensions(info, config)
    filters = [f"scale={w}:{h}:flags=lanczos", "setsar=1"]
    if config["sharpen"]:
        filters.append(f"unsharp=5:5:{config['sharpen']}:5:5:0")
    return filters + ["format=yuv420p"]


class Cancelled(Exception):
    pass


class Job:
    def __init__(self, info, config, preview):
        self.id = uuid.uuid4().hex
        self.info, self.config, self.preview = info, config, preview
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.process = None
        self.state = {"id": self.id, "status": "queued", "stage": "Queued",
                      "progress": 0, "error": None}
        self.files = {}
        self.log = DATA / (self.id + ".log")

    def update(self, **values):
        with self.lock:
            self.state.update(values)

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def stop(self):
        self.cancel.set()
        with self.lock:
            if self.process and self.process.poll() is None:
                self.process.terminate()

    def run(self, args, stage, begin, end, duration):
        if self.cancel.is_set():
            raise Cancelled()
        self.update(stage=stage, progress=begin)
        with self.log.open("a") as log:
            log.write("\n" + repr(args) + "\n")
            log.flush()
            process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=log, text=True)
            with self.lock:
                self.process = process
                if self.cancel.is_set():
                    process.terminate()
            try:
                for line in process.stdout:
                    if line.startswith("out_time_us="):
                        try:
                            elapsed = int(line.strip().split("=", 1)[1]) / 1_000_000
                        except ValueError:
                            continue
                        self.update(progress=round(begin + (end - begin) *
                                                   min(1, max(0, elapsed / duration)), 1))
                code = process.wait()
            finally:
                process.stdout.close()
                with self.lock:
                    self.process = None
            if self.cancel.is_set():
                raise Cancelled()
            if code:
                with self.log.open("rb") as errors:
                    errors.seek(max(0, self.log.stat().st_size - 5000))
                    tail = errors.read().decode("utf-8", errors="replace")
                raise UserError(f"{stage} failed (exit {code}).\n{tail}")

    def execute(self):
        pending_output = None
        try:
            DATA.mkdir(parents=True, exist_ok=True)
            OUTPUTS.mkdir(parents=True, exist_ok=True)
            ffmpeg = executable("ffmpeg")
            if not ffmpeg:
                raise UserError("FFmpeg is missing. Install it with: brew install ffmpeg")
            self.update(status="running")
            info, config = self.info, self.config
            duration = min(config["seconds"], info["duration"] - config["start"]) if self.preview else info["duration"]
            seek = ["-ss", str(config["start"])] if self.preview else []
            limit = ["-t", str(duration)] if self.preview else []
            base = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1"]
            audio = ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "192k"]
            encode = ["-c:v", "libx264", "-preset", "medium", "-crf", "18",
                      "-movflags", "+faststart"]
            final = OUTPUTS / f"{Path(info['name']).stem}-{self.id[:10]}.mp4"
            if self.preview:
                final = DATA / f"{self.id}-enhanced.mp4"
            pending_output = final.with_suffix(".partial.mp4")
            with tempfile.TemporaryDirectory(prefix="videoupscale-") as temporary:
                work = Path(temporary)
                source = info["path"]
                if self.preview:
                    original = DATA / f"{self.id}-original.mp4"
                    self.run(base + seek + ["-i", source] + limit +
                             ["-map", f"0:{info['video_index']}", "-vf",
                              "scale=trunc(iw*sar/2)*2:trunc(ih/2)*2,setsar=1,format=yuv420p"] +
                             audio + encode + [str(original)],
                             "Creating source preview", 0, 15, duration)
                    self.files["original"] = original
                filters = cleanup_filters(info, config)
                begin = 15 if self.preview else 0
                if config["backend"] == "ai":
                    ai = executable("realesrgan-ncnn-vulkan")
                    if not ai:
                        raise UserError("Real-ESRGAN is not installed or not on PATH.")
                    model_dir = os.environ.get("REALESRGAN_MODELS")
                    if not model_dir or not all(
                            (Path(model_dir).expanduser() / ("realesrgan-x4plus" + ext)).is_file()
                            for ext in (".param", ".bin")):
                        raise UserError("Set REALESRGAN_MODELS to the folder containing "
                                        "realesrgan-x4plus.param and realesrgan-x4plus.bin.")
                    frames, enhanced = work / "frames", work / "enhanced"
                    frames.mkdir()
                    enhanced.mkdir()
                    w, h = dimensions(info, dict(config, scale=1))
                    extract = filters + [f"scale={w}:{h}:flags=lanczos", "setsar=1"]
                    self.run(base + seek + ["-i", source] + limit +
                             ["-map", f"0:{info['video_index']}", "-vf", ",".join(extract),
                              "-r", str(output_fps(info, config)), "-fps_mode", "cfr",
                              str(frames / "%09d.png")],
                             "Extracting cleaned frames (AI uses temporary disk space)", begin, 30, duration)
                    self.run([ai, "-i", str(frames), "-o", str(enhanced), "-n",
                              "realesrgan-x4plus", "-s", "4", "-f", "png", "-m",
                              str(Path(model_dir).expanduser()), "-t", "256"],
                             "AI upscaling (progress unavailable during this stage)", 30, 80, duration)
                    expected = {p.name for p in frames.glob("*.png")}
                    actual = {p.name for p in enhanced.glob("*.png")}
                    if not expected or expected != actual:
                        raise UserError("Real-ESRGAN did not return every input frame.")
                    args = base + ["-framerate", str(output_fps(info, config)),
                                   "-start_number", "1", "-i", str(enhanced / "%09d.png")]
                    args += seek + ["-i", source] + limit
                    args += ["-map", "0:v:0", "-map", "1:a:0?", "-c:a", "aac",
                             "-b:a", "192k", "-vf", ",".join(finish_filters(info, config))]
                    begin = 80
                else:
                    args = base + seek + ["-i", source] + limit
                    args += ["-map", f"0:{info['video_index']}", "-vf",
                             ",".join(filters + finish_filters(info, config))] + audio
                self.run(args + encode + [str(pending_output)],
                         "Encoding enhanced video", begin, 99, duration)
                if self.cancel.is_set():
                    raise Cancelled()
                pending_output.replace(final)
                self.files["enhanced"] = final
                self.update(status="done", stage="Complete", progress=100,
                            output=str(final), media={
                                key: f"/media/{self.id}/{key}" for key in self.files})
        except Cancelled:
            self.update(status="cancelled", stage="Cancelled")
        except Exception as error:
            # Keep unexpected failures visible in both the job and server console.
            import traceback
            traceback.print_exc()
            self.update(status="error", stage="Failed", error=str(error))
        finally:
            if pending_output and pending_output.exists():
                pending_output.unlink()
            if self.snapshot()["status"] != "done":
                for path in self.files.values():
                    path.unlink(missing_ok=True)
                self.files.clear()


class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address):
        super().__init__(address, Handler)
        self.token = secrets.token_urlsafe(32)
        self.jobs = {}
        self.jobs_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(fmt % args)

    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def trusted(self, mutate=False):
        expected = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != expected:
            self.send_error(403, "Invalid host")
            return False
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + expected:
            self.send_error(403, "Invalid origin")
            return False
        if mutate and self.headers.get("X-App-Token") != self.server.token:
            self.send_error(403, "Invalid app token")
            return False
        return True

    def do_GET(self):
        if not self.trusted():
            return
        path = urlparse(self.path).path
        if path == "/api/status":
            return self.send_json({"engines": engines(), "token": self.server.token,
                                   "output_dir": str(OUTPUTS)})
        if path.startswith("/api/jobs/"):
            job = self.server.jobs.get(path.rsplit("/", 1)[-1])
            return self.send_json(job.snapshot() if job else {"error": "Job not found"},
                                  200 if job else 404)
        if path.startswith("/media/"):
            parts = path.split("/")
            job = self.server.jobs.get(parts[2]) if len(parts) == 4 else None
            file = job.files.get(parts[3]) if job and job.snapshot()["status"] == "done" else None
            if file and file.is_file():
                return self.send_file(file, ranges=True)
            return self.send_error(404)
        assets = {"/": ROOT / "web" / "index.html",
                  "/app.js": ROOT / "web" / "app.js",
                  "/style.css": ROOT / "web" / "style.css"}
        if path in assets:
            return self.send_file(assets[path])
        self.send_error(404)

    def send_file(self, path, ranges=False):
        size = path.stat().st_size
        start, end = 0, size - 1
        partial = False
        header = self.headers.get("Range") if ranges else None
        if header:
            try:
                unit, span = header.split("=", 1)
                left, right = span.split("-", 1)
                if unit != "bytes" or "," in span:
                    raise ValueError()
                if left:
                    start, end = int(left), min(int(right), size - 1) if right else size - 1
                else:
                    suffix = int(right)
                    if suffix <= 0:
                        raise ValueError()
                    start = max(0, size - suffix)
                if not 0 <= start <= end < size:
                    raise ValueError()
                partial = True
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "media-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        if ranges:
            self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with path.open("rb") as file:
                file.seek(start)
                remaining = end - start + 1
                while remaining:
                    chunk = file.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Browsers routinely close video range requests when seeking.

    def do_POST(self):
        if not self.trusted(mutate=True):
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 16_384:
                raise UserError("Request too large.")
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise UserError("Expected a JSON object.")
            path = urlparse(self.path).path
            if path == "/api/pick":
                result = subprocess.run([
                    "/usr/bin/osascript", "-e",
                    'try\nPOSIX path of (choose file with prompt "Choose an AVI or MP4 video")\n'
                    'on error number -128\nreturn ""\nend try'
                ], capture_output=True, text=True)
                if result.returncode:
                    raise UserError(result.stderr.strip())
                picked = result.stdout.rstrip("\n")
                return self.send_json({"path": picked})
            if path == "/api/probe":
                return self.send_json(probe(source_path(data.get("path"))))
            if path == "/api/jobs":
                info = probe(source_path(data.get("path")))
                config = settings(data, info)
                if not isinstance(data.get("preview", True), bool):
                    raise UserError("preview must be true or false.")
                if config["backend"] == "ai" and not executable("realesrgan-ncnn-vulkan"):
                    raise UserError("Real-ESRGAN is missing. Choose the FFmpeg engine instead.")
                with self.server.jobs_lock:
                    if any(j.snapshot()["status"] in ("queued", "running")
                           for j in self.server.jobs.values()):
                        return self.send_json({"error": "Wait for the active job or cancel it."}, 409)
                    job = Job(info, config, data.get("preview", True))
                    self.server.jobs[job.id] = job
                    threading.Thread(target=job.execute, daemon=True).start()
                return self.send_json(job.snapshot(), 202)
            if path.startswith("/api/cancel/"):
                job = self.server.jobs.get(path.rsplit("/", 1)[-1])
                if not job:
                    return self.send_json({"error": "Job not found."}, 404)
                job.stop()
                return self.send_json({"ok": True})
            self.send_json({"error": "Not found"}, 404)
        except (UserError, ValueError, OSError) as error:
            self.send_json({"error": str(error)}, 400)
        except Exception:
            import traceback
            traceback.print_exc()
            self.send_json({"error": "Unexpected server error. See the terminal for details."}, 500)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    server = AppServer(("127.0.0.1", args.port))
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"VideoUpscale: {url}\nPress Ctrl+C to stop. All processing stays on this Mac.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping active jobs...")
    finally:
        for job in server.jobs.values():
            job.stop()
        server.server_close()


if __name__ == "__main__":
    main()
