"use strict";
const $ = (id) => document.getElementById(id);
let token = "", info = null, activeJob = null, busy = false, capabilities = {};
let inspectedPath = "";

function error(message) {
  $("error").textContent = message;
  $("error").hidden = !message;
}

async function api(path, data) {
  const response = await fetch(path, data === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json", "X-App-Token": token },
    body: JSON.stringify(data),
  });
  if (!response.headers.get("content-type")?.includes("application/json")) {
    throw new Error(`Server returned HTTP ${response.status}. Reload the app and try again.`);
  }
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}

function buttons() {
  const ready = info && $("path").value.trim() === inspectedPath && capabilities.ffmpeg && capabilities.ffprobe;
  $("preview").disabled = busy || !ready;
  $("export").disabled = busy || !ready;
  $("pick").disabled = busy || !token;
  $("inspect").disabled = busy || !token;
  for (const id of ["path", "backend", "scale", "deinterlace", "denoise", "sharpen", "start", "seconds"]) {
    $(id).disabled = busy;
  }
  if ($("backend").value === "ai") $("scale").disabled = true;
}

async function inspect() {
  error("");
  info = null;
  buttons();
  try {
    const result = await api("/api/probe", { path: $("path").value.trim() });
    info = result;
    inspectedPath = result.path;
    $("path").value = result.path;
    $("start").max = Math.max(0, result.duration - 0.01);
    $("start").value = 0;
    $("metadata").textContent = `${result.width} x ${result.height} / ${(result.duration / 60).toFixed(1)} min / field order: ${result.field_order} / ${result.audio ? "audio included" : "no audio"}`;
  } catch (e) { error(e.message); $("metadata").textContent = "Unable to inspect this video."; }
  buttons();
}

$("pick").onclick = async () => {
  error("");
  $("pick").disabled = true;
  try {
    const selected = await api("/api/pick", {});
    if (selected.path) { $("path").value = selected.path; await inspect(); }
  } catch (e) { error(e.message); }
  buttons();
};
$("inspect").onclick = inspect;
$("path").oninput = buttons;
for (const id of ["denoise", "sharpen"]) {
  $(id).oninput = () => { $(`${id}-value`).textContent = $(id).value; };
}
$("backend").onchange = () => {
  if ($("backend").value === "ai") $("scale").value = "4";
  buttons();
};

async function start(preview) {
  error("");
  $("result").hidden = true;
  if (!preview && $("backend").value === "ai" &&
      !confirm("AI exports can take hours and use substantial temporary disk space. Have you checked a short preview first?")) return;
  busy = true;
  buttons();
  try {
    const options = { path: inspectedPath, preview };
    for (const id of ["backend", "scale", "deinterlace", "denoise", "sharpen", "start", "seconds"]) {
      options[id] = $(id).value;
    }
    const job = await api("/api/jobs", options);
    activeJob = job.id;
    $("job-panel").hidden = false;
    $("cancel").hidden = false;
    $("stage").textContent = "Queued";
    $("progress").value = 0;
    poll(job.id, preview, options);
  } catch (e) { error(e.message); busy = false; buttons(); }
}

async function poll(id, preview, options) {
  try {
    const job = await api(`/api/jobs/${id}`);
    $("stage").textContent = job.stage;
    $("progress").value = job.progress;
    $("job-detail").textContent = `${job.progress}% / You may leave this tab open while processing.`;
    if (["queued", "running"].includes(job.status)) {
      setTimeout(() => poll(id, preview, options), 800);
      return;
    }
    activeJob = null;
    busy = false;
    $("cancel").hidden = true;
    buttons();
    if (job.status === "error") error(job.error);
    if (job.status === "done") {
      if (preview) {
        for (const name of ["original", "enhanced"]) {
          $(name).src = job.media[name];
          $(name).load();
        }
        $("enhanced").muted = true;
        $("empty").hidden = true;
        $("players").hidden = false;
        $("enhanced-label").textContent = `${options.scale}x / ${options.backend === "ai" ? "Real-ESRGAN" : "Lanczos"}`;
      } else {
        $("result").textContent = `Export saved: ${job.output}`;
        $("result").hidden = false;
      }
    }
  } catch (e) {
    error(`Lost connection to the job: ${e.message}. Processing may still be running. Retrying...`);
    setTimeout(() => poll(id, preview, options), 3000);
  }
}

$("preview").onclick = () => start(true);
$("export").onclick = () => start(false);
$("cancel").onclick = async () => {
  if (!activeJob) return;
  try { await api(`/api/cancel/${activeJob}`, {}); }
  catch (e) { error(e.message); }
};
$("play-both").onclick = async () => {
  try {
    for (const name of ["original", "enhanced"]) { $(name).pause(); $(name).currentTime = 0; }
    await Promise.all([$("original").play(), $("enhanced").play()]);
  } catch (e) { error(`Playback failed: ${e.message}`); }
};

async function initialize() {
  try {
    const status = await api("/api/status");
    token = status.token;
    capabilities = status.engines;
    const missing = ["ffmpeg", "ffprobe"].filter((name) => !capabilities[name]);
    $("engines").textContent = missing.length
      ? `Missing ${missing.join(" and ")}. Install with: brew install ffmpeg. Then reload this page.`
      : `FFmpeg ready. ${capabilities["realesrgan-ncnn-vulkan"] ? "Real-ESRGAN executable detected; model setup is required for AI." : "Optional AI engine not installed; classic restoration is ready."}`;
    $("backend").options[1].disabled = !capabilities["realesrgan-ncnn-vulkan"];
    $("output-dir").textContent = `Exports: ${status.output_dir}`;
  } catch (e) { error(e.message); }
  buttons();
}
initialize();
