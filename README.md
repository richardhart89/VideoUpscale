# VideoUpscale

A local browser application for restoring and upscaling AVI and MP4 footage on
macOS. No uploads, accounts, Python packages, or cloud services are required.
The original video is never overwritten.

## Run

Requires Python 3.9+ and an FFmpeg build with `libx264`, `bwdif`, `hqdn3d`,
`scale`, and `unsharp` (the Homebrew build provides these).

```sh
brew install ffmpeg
python3 upscale.py
```

If Homebrew reports an unaccepted Xcode license, review/accept the license in
Xcode or with `sudo xcodebuild -license`, then retry the installation.

The app opens at http://127.0.0.1:8765. Keep the terminal running; Ctrl+C stops
the server and cancels active processing. Use `--port 8766` for another port or
`--no-browser` to launch without opening a browser.

The included VS Code **Run VideoUpscale** task uses this Mac's existing
`/opt/anaconda3/bin/python3` interpreter. If `python3` in your Terminal invokes a
broken Apple developer-tools launcher, use that absolute interpreter path too.
On another computer, update the task to your installed Python interpreter.

1. Choose a video with the native macOS picker, or paste its absolute path and
   click **Inspect**.
2. Select 1x cleanup, 2x, or 4x scaling and adjust cleanup/sharpening.
3. Generate a short preview containing motion and compare both players.
4. Export the complete video to `~/Movies/VideoUpscale`.

Output is high-quality H.264 MP4 (CRF 18) with the first audio track encoded to
AAC. Source aspect ratio is retained, including non-square DV/DVD pixels.
Preview files and diagnostic logs persist in
`~/Library/Application Support/VideoUpscale`; remove unneeded files there
when the server is stopped. AI frame directories are temporary and are removed
after completion, failure, or cancellation.

## Choosing a restoration approach

There is no universally best upscaler: compare the same moving clip before
processing a whole video.

| Engine | Good fit | Trade-offs |
| --- | --- | --- |
| FFmpeg Lanczos + cleanup | Quick, predictable restoration; natural textures | Conventional resampling, not learned detail reconstruction |
| Real-ESRGAN x4plus | Optional learned restoration/upscaling | Slow, large temporary storage, possible invented texture and flicker |
| Topaz Video (external, not integrated) | Paid alternative with temporally aware video models | Proprietary; evaluate its results and terms separately |

The pipeline is **deinterlace -> denoise -> upscale -> gently sharpen**.
Comb-shaped edges around motion are typically interlacing, not ordinary noise.
Auto mode trusts source field-order metadata; it does not visually detect lines.
For incorrectly flagged footage, compare forced top-first and bottom-first modes.
The correct field order should produce smooth forward motion. Double-rate
deinterlacing preserves field motion (e.g. 25 fps interlaced becomes 50 fps).
Persistent horizontal bands, telecine, moire, and baked-in scanlines are different
problems and are not automatically repaired.

## Optional AI engine

Install the macOS executable and its models from the
[Real-ESRGAN ncnn Vulkan project](https://github.com/xinntao/Real-ESRGAN-ncnn-vulkan).
Check compatibility with your Mac and the downloaded release; this application
does not automatically install or download executables or weights.

The x4plus model is used for general/live-action footage, rather than the
executable's default anime model. This integration supports **4x only**.
Point to the executable and the folder containing both
`realesrgan-x4plus.param` and `realesrgan-x4plus.bin`:

```sh
export REALESRGAN_NCNN_VULKAN_PATH="/absolute/path/realesrgan-ncnn-vulkan"
export REALESRGAN_MODELS="/absolute/path/models"
python3 upscale.py
```

Alternatively place the executable on `PATH`. `FFMPEG_PATH` and `FFPROBE_PATH`
can override those binaries too.

AI processing extracts cleaned PNG frames, upscales them, verifies that every
frame was returned, and remuxes newly encoded audio from the original input.
Variable-frame-rate sources are normalized to their reported average frame
rate for AI processing. This can duplicate/drop frames; preview motion and
audio sync. AI progress is stage-based, not a frame-accurate time estimate.
There is no temporal AI model in this version.

**Disk space:** even a short source can require many GB of PNGs. Both original
and enlarged frames coexist during AI processing. Start with a 5-second preview
and ensure ample free space on the system temporary volume. One job runs at a
time. Cancellation stops the current engine and removes its temporary frames.

## Scope and limitations

- AVI/MP4 input codec support depends on FFmpeg.
- SDR-only output; detected HDR/10-bit and rotated sources are rejected rather
  than silently mishandled. This is not a color-managed archival workflow.
- One video stream and first audio stream only; subtitles, extra audio,
  chapters, and container-specific metadata are not preserved.
- No batch queue, resume across server restarts, or automatic artifact diagnosis.
- Before/after players start together but are not frame-locked.
- Local single-user tool: binds only to IPv4 loopback, checks Host/Origin, and
  requires an app token for actions. Do not expose it through a public proxy.
- Review FFmpeg build licensing and engine/model licensing before redistribution.

## Development

```sh
python3 -m unittest discover -s tests -v
```

Unit tests need no engines. Integration tests run when FFmpeg and FFprobe are
available; they synthesize small test videos rather than using personal footage.

### References

- [FFmpeg filter documentation](https://ffmpeg.org/ffmpeg-filters.html):
  `bwdif`, `hqdn3d`, `scale`, `unsharp`.
- [Real-ESRGAN ncnn Vulkan](https://github.com/xinntao/Real-ESRGAN-ncnn-vulkan):
  executable options, model selection, platform instructions and license.
- [Topaz Video](https://www.topazlabs.com/topaz-video):
  external commercial comparison candidate, not a bundled dependency.
