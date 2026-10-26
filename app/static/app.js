const $ = (id) => document.getElementById(id);

const MODE_HINTS = {
  ascii: "ASCII characters chosen by shape, so edges get / | _ and friends.",
  hybrid: "ASCII for smooth areas, plus braille and block glyphs where they trace detail better.",
  braille: "A dot-matrix look: every character is a 2×4 grid of braille dots.",
  ramp: "One character per brightness level, from darkest to brightest. The classic look.",
  text: "Fills the picture with your phrase. Brightness and colour draw the image.",
};
const RANGES = ["columns", "font_size", "detail", "color_boost", "saturation", "bg_strength"];
const TEXTS = ["custom_chars", "text"];
const VIDEO_EXTS = ["mp4", "mov", "webm", "mkv", "avi", "m4v"];
const IMAGE_EXTS = ["png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff", "gif"];
const CHAT_HINT = $("chatHint").textContent;

const segValues = (selector) => Object.fromEntries(
  [...document.querySelectorAll(selector)].map((s) => [s.dataset.name, s.querySelector(".on").dataset.value]));

const DEFAULTS = {
  ranges: Object.fromEntries(RANGES.map((id) => [id, $(id).defaultValue])),
  texts: Object.fromEntries(TEXTS.map((id) => [id, $(id).defaultValue])),
  segs: segValues(".seg:not(.local)"),
};

const limits = { max_upload_mb: 100, max_video_seconds: 0 };
const themeKinds = { original: "original" };

const state = {
  file: null,
  animated: false,
  previewSource: null,
  previewBlob: null,
  previewUrl: null,
  previewAbort: null,
  debounce: null,
  polling: null,
  renderStart: 0,
  shownJob: null,
  resultUrl: null,
  settings: { ...DEFAULTS.segs },
  local: segValues(".seg.local"),
};

function settingsForm(file) {
  const form = new FormData();
  form.append("file", file, file.name || "frame.png");
  for (const [k, v] of Object.entries(state.settings)) form.append(k, v);
  for (const id of [...RANGES, ...TEXTS]) form.append(id, $(id).value);
  return form;
}

function showError(msg) {
  $("error").textContent = msg || "";
  $("error").hidden = !msg;
}

function showNote(msg) {
  $("fileNote").textContent = msg || "";
  $("fileNote").hidden = !msg;
}

async function errorText(res) {
  try {
    const body = await res.json();
    return body.detail || res.statusText;
  } catch {
    return res.statusText;
  }
}

function formatDuration(seconds) {
  seconds = Math.max(0, Math.round(seconds));
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

function syncControls() {
  for (const id of [...RANGES, "chatColumns"]) $(id + "Out").textContent = $(id).value;
  const { mode, ramp, theme, background, focus } = state.settings;
  $("modeHint").textContent = MODE_HINTS[mode];
  $("rampRow").hidden = mode !== "ramp";
  $("customRow").hidden = !(mode === "ramp" && ramp === "custom");
  $("textRow").hidden = mode !== "text";
  const shaped = mode === "ascii" || mode === "hybrid";
  $("detailRow").classList.toggle("disabled", !shaped);
  $("detail").disabled = !shaped;
  $("saturationRow").hidden = themeKinds[theme] === "gradient";
  $("backgroundRow").hidden = theme !== "original";
  $("tintRow").hidden = theme !== "original" || background !== "tinted";
  $("focusHint").hidden = focus === "off";
  $("reset").disabled = isDefault();
  const label = state.local.format.toUpperCase();
  $("renderVideo").textContent = `Render ${label}`;
  $("downloadVideo").textContent = `Download ${label}`;
  $("downloadVideo").download = `ascii.${state.local.format}`;
}

function isDefault() {
  return RANGES.every((id) => $(id).value === DEFAULTS.ranges[id])
    && TEXTS.every((id) => $(id).value === DEFAULTS.texts[id])
    && Object.entries(DEFAULTS.segs).every(([name, value]) => state.settings[name] === value);
}

function setSeg(name, value) {
  const seg = document.querySelector(`.seg[data-name="${name}"]`);
  seg.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.value === value));
  if (seg.classList.contains("local")) state.local[name] = value;
  else state.settings[name] = value;
}

document.querySelectorAll(".seg").forEach((seg) => {
  seg.addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    setSeg(seg.dataset.name, btn.dataset.value);
    if (!seg.classList.contains("local")) return onSettingsChange();
    if (seg.dataset.name === "format") resetVideoResult();
    syncControls();
  });
});
[...RANGES, ...TEXTS].forEach((id) => $(id).addEventListener("input", onSettingsChange));
$("chatColumns").addEventListener("input", syncControls);

$("reset").addEventListener("click", () => {
  RANGES.forEach((id) => { $(id).value = DEFAULTS.ranges[id]; });
  TEXTS.forEach((id) => { $(id).value = DEFAULTS.texts[id]; });
  Object.entries(DEFAULTS.segs).forEach(([name, value]) => setSeg(name, value));
  onSettingsChange();
});

function onSettingsChange() {
  syncControls();
  resetVideoResult();
  clearTimeout(state.debounce);
  state.debounce = setTimeout(updatePreview, 300);
}

fetch("/api/options")
  .then((r) => r.json())
  .then(({ themes }) => {
    for (const [key, theme] of Object.entries(themes)) {
      themeKinds[key] = theme.kind;
      if (key === "original") continue;
      const swatch = document.createElement("i");
      swatch.className = "swatch";
      swatch.style.background = `linear-gradient(135deg, ${theme.background} 0 30%, ${theme.colors.join(", ")})`;
      const btn = Object.assign(document.createElement("button"), { type: "button" });
      btn.dataset.value = key;
      btn.append(swatch, theme.label);
      $("themes").append(btn);
    }
    syncControls();
  })
  .catch(() => {});

const drop = $("drop");
drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", (e) => {
  e.preventDefault();
  drop.classList.remove("over");
  if (e.dataTransfer.files[0]) loadFile(e.dataTransfer.files[0]);
});
$("file").addEventListener("change", (e) => e.target.files[0] && loadFile(e.target.files[0]));
$("sample").addEventListener("click", async () => {
  const blob = await (await fetch("samples/astronaut.png")).blob();
  loadFile(new File([blob], "astronaut.png", { type: blob.type }));
});

function limitsText() {
  const seconds = limits.max_video_seconds ? ` · videos ≤ ${limits.max_video_seconds}s` : "";
  return `Images, GIFs, videos · max ${limits.max_upload_mb} MB${seconds}`;
}

fetch("/api/limits")
  .then((r) => r.json())
  .then((l) => {
    Object.assign(limits, l);
    if (!state.file) $("dropSub").textContent = limitsText();
  })
  .catch(() => {});
$("dropSub").textContent = limitsText();

async function loadFile(file) {
  showError("");
  showNote("");
  resetVideoResult();
  const ext = file.name.split(".").pop().toLowerCase();
  const isVideo = file.type.startsWith("video/") || VIDEO_EXTS.includes(ext);
  const isGif = file.type === "image/gif" || ext === "gif";
  if (!isVideo && !file.type.startsWith("image/") && !IMAGE_EXTS.includes(ext)) {
    showError("Please choose an image, GIF or video.");
    return;
  }
  if (file.size > limits.max_upload_mb * 1048576) {
    showError(`This file is ${(file.size / 1048576).toFixed(1)} MB. The limit is ${limits.max_upload_mb} MB.`);
    return;
  }
  state.file = file;
  state.animated = isVideo || isGif;
  setSeg("format", isGif ? "gif" : "mp4");
  syncControls();
  $("dropTitle").textContent = file.name;
  $("dropSub").textContent = `${(file.size / 1048576).toFixed(1)} MB · click to change`;

  const url = URL.createObjectURL(file);
  let media;
  if (isVideo) {
    media = document.createElement("video");
    Object.assign(media, { src: url, controls: true, muted: true, loop: true, playsInline: true });
  } else {
    media = document.createElement("img");
    media.src = url;
  }
  $("original").replaceChildren(media);

  $("downloadPng").hidden = state.animated;
  $("renderVideo").hidden = !state.animated;
  $("formatRow").hidden = !state.animated;
  $("share").hidden = false;
  $("resultLabel").textContent = state.animated ? "ASCII · first frame preview" : "ASCII";
  $("resultMeta").textContent = "";

  try {
    if (isVideo) {
      const { blob, duration } = await videoFrame(url);
      state.previewSource = blob;
      describeVideo(duration);
    } else {
      state.previewSource = isGif ? await gifFrame(url) : file;
    }
  } catch {
    state.previewSource = null;
    $("share").hidden = true;
    if (!state.animated) {
      showError("Could not read this image.");
      return;
    }
    $("result").replaceChildren(Object.assign(document.createElement("p"), {
      className: "empty",
      textContent: "Your browser can't preview this format, but rendering still works.",
    }));
    return;
  }
  updatePreview();
}

function describeVideo(duration) {
  if (!isFinite(duration)) return;
  const max = limits.max_video_seconds;
  if (max && duration > max) {
    showNote(`This video is ${formatDuration(duration)} long. Only the first ${formatDuration(max)} will be converted.`);
  } else {
    showNote(`${formatDuration(duration)} long, so up to ${Math.ceil(duration * 30)} frames to convert.`);
  }
}

function canvasBlob(source, w, h) {
  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  canvas.getContext("2d").drawImage(source, 0, 0, w, h);
  return new Promise((resolve, reject) =>
    canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("toBlob failed"))), "image/png"));
}

function videoFrame(url) {
  return new Promise((resolve, reject) => {
    const v = document.createElement("video");
    v.muted = true;
    v.preload = "auto";
    v.onloadedmetadata = () => { v.currentTime = Math.min(0.5, v.duration / 2 || 0); };
    v.onseeked = () => canvasBlob(v, v.videoWidth, v.videoHeight)
      .then((blob) => resolve({ blob, duration: v.duration }), reject);
    v.onerror = reject;
    setTimeout(() => reject(new Error("timeout")), 8000);
    v.src = url;
  });
}

function gifFrame(url) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => canvasBlob(img, img.naturalWidth, img.naturalHeight).then(resolve, reject);
    img.onerror = reject;
    img.src = url;
  });
}

async function updatePreview() {
  if (!state.previewSource) return;
  state.previewAbort?.abort();
  const abort = (state.previewAbort = new AbortController());
  const frame = $("result");
  frame.classList.add("busy");
  showError("");
  try {
    const res = await fetch("/api/convert/image", {
      method: "POST",
      body: settingsForm(state.previewSource),
      signal: abort.signal,
    });
    if (!res.ok) throw new Error(await errorText(res));
    const blob = await res.blob();
    if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
    state.previewBlob = blob;
    state.previewUrl = URL.createObjectURL(blob);

    const img = new Image();
    img.src = state.previewUrl;
    await img.decode();
    frame.replaceChildren(img);
    $("resultMeta").textContent = `${img.naturalWidth}×${img.naturalHeight}`;
    $("downloadPng").href = state.previewUrl;
  } catch (e) {
    if (e.name !== "AbortError") showError(e.message);
  } finally {
    if (state.previewAbort === abort) frame.classList.remove("busy");
  }
}

$("result").addEventListener("click", (e) => {
  if (e.target.tagName === "IMG") $("result").classList.toggle("zoom");
});

function downloadBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  Object.assign(document.createElement("a"), { href: url, download: name }).click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function legacyCopy(text) {
  const area = Object.assign(document.createElement("textarea"), { value: text, readOnly: true });
  area.style.cssText = "position:fixed;opacity:0;pointer-events:none";
  document.body.append(area);
  area.select();
  try {
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    area.remove();
  }
}

function flash(btn, label, ok) {
  btn.textContent = ok ? "Copied ✓" : label;
  setTimeout(() => { btn.textContent = label; }, 1500);
}

async function copyResponse(btn, label, request, withHtml) {
  btn.disabled = true;
  btn.textContent = "Copying…";
  showError("");
  try {
    let copied = false;
    try {
      const items = { "text/plain": request.then((d) => new Blob([d.text], { type: "text/plain" })) };
      if (withHtml) items["text/html"] = request.then((d) => new Blob([d.html], { type: "text/html" }));
      await navigator.clipboard.write([new ClipboardItem(items)]);
      copied = true;
    } catch {
      const { text } = await request;
      copied = await navigator.clipboard.writeText(text).then(() => true, () => legacyCopy(text));
      if (!copied) {
        downloadBlob(new Blob([text], { type: "text/plain" }), "ascii.txt");
        showError("Your browser blocked clipboard access, so the text was downloaded as ascii.txt instead.");
      }
    }
    flash(btn, label, copied);
    return await request;
  } catch (e) {
    showError(e.message || "Could not copy to the clipboard.");
    btn.textContent = label;
  } finally {
    btn.disabled = false;
  }
}

function postJson(url, form) {
  return fetch(url, { method: "POST", body: form }).then(async (res) => {
    if (!res.ok) throw new Error(await errorText(res));
    return res.json();
  });
}

$("copyText").addEventListener("click", () => {
  if (!state.previewSource) return;
  copyResponse($("copyText"), "Copy as text", postJson("/api/convert/text", settingsForm(state.previewSource)), true);
});

$("copyChat").addEventListener("click", async () => {
  if (!state.previewSource) return;
  const form = settingsForm(state.previewSource);
  form.append("chat_columns", $("chatColumns").value);
  form.append("chat_style", state.local.chatStyle);
  form.append("chat_light", state.local.chatTheme === "light");
  const data = await copyResponse($("copyChat"), "Copy text for chat", postJson("/api/convert/chat", form), false);
  if (data) {
    const warning = data.chars > 2000 ? " Too long for Discord's 2000 limit, so lower the width." : "";
    $("chatHint").textContent = `Copied ${data.chars} characters.${warning} ${CHAT_HINT}`;
  }
});

$("copyImage").addEventListener("click", async () => {
  const btn = $("copyImage");
  if (!state.previewBlob) return;
  showError("");
  try {
    await navigator.clipboard.write([new ClipboardItem({ "image/png": state.previewBlob })]);
    flash(btn, "Copy image", true);
  } catch {
    downloadBlob(state.previewBlob, "ascii.png");
    showError("Your browser blocked copying images, so it was downloaded as ascii.png instead.");
  }
});

function resetVideoResult() {
  clearInterval(state.polling);
  state.polling = null;
  $("progress").hidden = true;
  $("downloadVideo").hidden = true;
  $("renderVideo").disabled = false;
}

$("renderVideo").addEventListener("click", async () => {
  if (!state.file) return;
  resetVideoResult();
  showError("");
  $("renderVideo").disabled = true;
  setProgress(0, "uploading…");
  const form = settingsForm(state.file);
  form.append("format", state.local.format);
  try {
    const { job_id } = await postJson("/api/convert/video", form);
    state.renderStart = 0;
    state.polling = setInterval(() => poll(job_id), 700);
  } catch (e) {
    showError(e.message);
    resetVideoResult();
  }
});

function setProgress(fraction, text) {
  $("progress").hidden = false;
  $("bar").style.width = `${Math.round(fraction * 100)}%`;
  $("progressText").textContent = text;
}

async function poll(jobId) {
  let job;
  try {
    const res = await fetch(`/api/jobs/${jobId}`);
    if (!res.ok) throw new Error(await errorText(res));
    job = await res.json();
  } catch (e) {
    showError(e.message);
    resetVideoResult();
    return;
  }
  if (job.status === "error") {
    showError(job.error);
    resetVideoResult();
  } else if (job.status === "done") {
    showResult(jobId, job.info);
  } else if (job.status === "running" && job.done) {
    state.renderStart ||= performance.now();
    const elapsed = (performance.now() - state.renderStart) / 1000;
    const left = job.done > 3 ? ` · ~${formatDuration((elapsed / job.done) * (job.total - job.done))} left` : "";
    setProgress(job.done / job.total, `frame ${job.done} / ${job.total}${left}`);
  } else {
    setProgress(0, job.status === "queued" ? "waiting for another render…" : "starting…");
  }
}

async function showResult(jobId, info) {
  clearInterval(state.polling);
  state.polling = null;
  if (state.shownJob === jobId) return;
  state.shownJob = jobId;
  setProgress(1, "saving to your device…");
  let url;
  try {
    const res = await fetch(`/api/jobs/${jobId}/result`);
    if (!res.ok) throw new Error(await errorText(res));
    if (state.resultUrl) URL.revokeObjectURL(state.resultUrl);
    url = state.resultUrl = URL.createObjectURL(await res.blob());
  } catch (e) {
    showError(e.message);
    resetVideoResult();
    return;
  }
  fetch(`/api/jobs/${jobId}`, { method: "DELETE" }).catch(() => {});
  setProgress(1, `saved to Downloads · ${info.frames} frames${info.audio ? " · audio kept" : ""}`);
  const media = info.format === "gif"
    ? Object.assign(document.createElement("img"), { src: url })
    : Object.assign(document.createElement("video"), { src: url, controls: true, autoplay: true, loop: true, playsInline: true });
  $("result").classList.remove("zoom");
  $("result").replaceChildren(media);
  $("resultLabel").textContent = `ASCII · ${info.format.toUpperCase()}`;
  $("resultMeta").textContent = `${info.width}×${info.height} · ${info.fps} fps`;
  $("downloadVideo").href = url;
  $("downloadVideo").download = `ascii.${info.format}`;
  $("downloadVideo").textContent = `Download ${info.format.toUpperCase()}`;
  $("downloadVideo").hidden = false;
  $("downloadVideo").click();
  $("renderVideo").disabled = false;
}

syncControls();
