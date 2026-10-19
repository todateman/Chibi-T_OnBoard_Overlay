// ecorun レイアウトエディタ(ビルド不要の ES module)
// 状態はブラウザ側に編集中のプリセット(JSON)として持ち、描画・検証・保存はサーバー API に任せる。
import { el, field, schemaParams } from "./form.js";

const $ = (id) => document.getElementById(id);
const SVGNS = "http://www.w3.org/2000/svg";
const COMMON = ["id", "enabled", "x", "y", "w", "h"];
const SQUARE_TYPES = new Set(["speed_gauge", "rpm_gauge"]);
const LS_KEY = "ecorun-gui";

const S = {
  presets: [], widgetTypes: [], typeMap: {}, schema: null,
  name: null, mtime: null, preset: null, savedJson: "",
  selected: null,                 // ウィジェット id。null なら全体設定
  history: [], future: [],
  issues: [], errors: [], skipped: [],
  csv: "", logInfo: null, ts: 0,
  sync: "", offset: 0, start: null, end: null,
  timemap: null,                  // OCR の区間ごとの同期 { key: 動画欄, segments: [{v0, v1, offset, wall}] }
  video: "", videoInfo: null, videoT: 0,
  output: "",                     // 書き出し先。空なら defaultOutput()
  snap: 10, showKeepout: true, showBoxes: true, showDisabled: false,
  playing: false,
};

// ---------------------------------------------------------------------
//  API
// ---------------------------------------------------------------------
async function api(method, path, body) {
  const res = await fetch(path, {
    method, headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let info = {};
    try { info = await res.json(); } catch { /* 本文なし */ }
    const err = new Error(info.message || info.detail || `${res.status} ${res.statusText}`);
    err.status = res.status; err.info = info;
    throw err;
  }
  const ct = res.headers.get("content-type") || "";
  return ct.includes("json") ? res.json() : res;
}

// ---------------------------------------------------------------------
//  ユーティリティ
// ---------------------------------------------------------------------
const clone = (o) => structuredClone(o);
const canvasW = () => S.preset?.canvas?.width ?? 1920;
const canvasH = () => S.preset?.canvas?.height ?? 1080;
const widget = (id) => S.preset?.widgets.find((w) => w.id === id);
const isDirty = () => S.preset && JSON.stringify(S.preset) !== S.savedJson;
const snap = (v) => Math.round(v / S.snap) * S.snap;
let statusTimer;
function status(msg, ms = 2500) {
  $("status").textContent = msg;
  clearTimeout(statusTimer);
  if (ms) statusTimer = setTimeout(() => ($("status").textContent = ""), ms);
}
function banner(msg, buttons = [], kind = "") {
  const b = $("banner");
  if (!msg) { b.hidden = true; b.replaceChildren(); return; }
  b.className = `banner ${kind}`;
  b.replaceChildren(el("span", {}, msg), ...buttons.map(([label, fn]) => el("button", { onclick: fn }, label)));
  b.hidden = false;
}
function saveLocal() {
  const keep = { name: S.name, csv: S.csv, output: S.output, sync: S.sync, offset: S.offset, start: S.start, end: S.end, timemap: S.timemap,
    video: S.video, snap: S.snap, showKeepout: S.showKeepout, showBoxes: S.showBoxes, showDisabled: S.showDisabled, ts: S.ts, videoT: S.videoT };
  try { localStorage.setItem(LS_KEY, JSON.stringify(keep)); } catch { /* 使えない環境 */ }
}
function loadLocal() {
  try { return JSON.parse(localStorage.getItem(LS_KEY) || "{}"); } catch { return {}; }
}
function dialog(msg, { input = null, ok = "OK" } = {}) {
  return new Promise((resolve) => {
    const d = $("dlg");
    $("dlgMsg").textContent = msg;
    $("dlgOk").textContent = ok;
    const inp = $("dlgInput");
    inp.hidden = input === null;
    inp.value = input ?? "";
    d.onclose = () => resolve(d.returnValue === "ok" ? (input === null ? true : inp.value.trim()) : null);
    d.showModal();
    if (input !== null) inp.select();
  });
}

// ---------------------------------------------------------------------
//  編集(履歴つき)
// ---------------------------------------------------------------------
function edit(fn, { render = true } = {}) {
  S.history.push(clone(S.preset));
  if (S.history.length > 200) S.history.shift();
  S.future = [];
  fn(S.preset);
  changed({ render });
}
function changed({ render = true, props = true } = {}) {
  $("dirty").hidden = !isDirty();
  $("btnUndo").disabled = !S.history.length;
  $("btnRedo").disabled = !S.future.length;
  if (S.selected && !widget(S.selected)) S.selected = null;
  drawList();
  drawSvg();
  if (props) drawProps();
  drawCommand();
  scheduleValidate();
  if (render) scheduleRender();
}
function undo() {
  if (!S.history.length) return;
  S.future.push(clone(S.preset));
  S.preset = S.history.pop();
  changed();
}
function redo() {
  if (!S.future.length) return;
  S.history.push(clone(S.preset));
  S.preset = S.future.pop();
  changed();
}

function uniqueId(base) {
  const used = new Set(S.preset.widgets.map((w) => w.id));
  if (!used.has(base)) return base;
  let k = 2;
  while (used.has(`${base}_${k}`)) k++;
  return `${base}_${k}`;
}
function newWidget(type) {
  const t = S.typeMap[type];
  const w = { id: uniqueId(type), type, enabled: true, ...t.default_box };
  for (const p of t.params) if (!(p.name in w) && p.default !== undefined) w[p.name] = clone(p.default);
  return w;
}

// ---------------------------------------------------------------------
//  プリセットの読み込み・保存
// ---------------------------------------------------------------------
async function refreshPresets() {
  S.presets = await api("GET", "/api/presets");
  const sel = $("presetSel");
  sel.replaceChildren(...S.presets.map((p) =>
    el("option", { value: p.name, selected: p.name === S.name },
      `${p.name}${p.title ? ` — ${p.title}` : ""}${p.valid ? "" : "(不正)"}`)));
}

async function loadPreset(name, { keepHistory = false, quiet = false } = {}) {
  try {
    const r = await api("GET", `/api/presets/${encodeURIComponent(name)}`);
    S.name = name; S.mtime = r.mtime; S.preset = r.preset;
    S.savedJson = JSON.stringify(r.preset);
    if (!keepHistory) { S.history = []; S.future = []; }
    banner(null);
    saveLocal();
    $("presetSel").value = name;
    changed();
    if (!quiet) status(`${name} を読み込みました`);
  } catch (e) {
    const errs = e.info?.errors?.map((x) => `${x.loc}: ${x.msg}`).join("\n") || "";
    banner(`${name} を読み込めません: ${e.message}${errs ? "\n" + errs : ""}`, [], "error");
  }
}

async function save({ force = false } = {}) {
  if (!S.preset) return;
  try {
    const r = await api("PUT", `/api/presets/${encodeURIComponent(S.name)}`, { preset: S.preset, base_mtime: S.mtime, force });
    S.mtime = r.mtime;
    S.savedJson = JSON.stringify(S.preset);
    banner(null);
    changed({ render: false, props: false });
    status(`${r.path} に保存しました`);
    refreshPresets();
  } catch (e) {
    if (e.status === 409) {
      banner("ファイルが外部(エディタや AI エージェント)で変更されています。", [
        ["上書き保存", () => save({ force: true })],
        ["外部の変更を読み込む(編集は破棄)", () => loadPreset(S.name)],
      ]);
    } else {
      banner(`保存できません: ${e.message} ${(e.info?.errors || []).map((x) => `${x.loc}: ${x.msg}`).join(" / ")}`, [], "error");
    }
  }
}

async function saveAs() {
  const name = await dialog("新しいプリセット名(英数字・_・-)", { input: `${S.name}_copy`, ok: "保存" });
  if (!name) return;
  if (!/^[A-Za-z0-9_-]+$/.test(name)) { status("プリセット名は英数字・_・- のみ使えます"); return; }
  if (S.presets.some((p) => p.name === name) && !(await dialog(`${name} は既にあります。上書きしますか?`, { ok: "上書き" }))) return;
  S.name = name; S.mtime = null;
  await save({ force: true });
  await refreshPresets();
  $("presetSel").value = name;
  saveLocal();
}

async function deletePreset() {
  if (!(await dialog(`プリセット ${S.name} を削除しますか?(presets/${S.name}.toml を消します)`, { ok: "削除" }))) return;
  await api("DELETE", `/api/presets/${encodeURIComponent(S.name)}`);
  await refreshPresets();
  if (S.presets.length) await loadPreset(S.presets[0].name);
}

// 外部での変更(AI エージェント・テキストエディタ)を受け取る
function watchEvents() {
  const es = new EventSource("/api/events");
  es.addEventListener("presets", async (ev) => {
    const { changed: names, mtimes } = JSON.parse(ev.data);
    await refreshPresets();
    if (!S.name || !names.includes(S.name)) return;
    const m = mtimes[S.name];
    if (m === undefined) {
      banner(`${S.name}.toml が削除されました。保存すると作り直します。`);
      S.mtime = null;
    } else if (S.mtime === null || Math.abs(m - S.mtime) > 1e-6) {
      if (!isDirty()) {
        await loadPreset(S.name, { keepHistory: true, quiet: true });
        status(`${S.name}.toml が外部で変更されたので読み込み直しました`, 5000);
      } else {
        banner(`${S.name}.toml が外部で変更されました。`, [
          ["外部の変更を読み込む(編集は破棄)", () => loadPreset(S.name, { keepHistory: true })],
          ["無視して編集を続ける", () => banner(null)],
        ]);
      }
    }
  });
}

// ---------------------------------------------------------------------
//  描画(サーバーで本番と同じ Pillow 描画)
// ---------------------------------------------------------------------
let renderTimer, renderBusy = false, renderAgain = false, lastUrl = null;
function scheduleRender(delay = 60) {
  clearTimeout(renderTimer);
  renderTimer = setTimeout(doRender, delay);
}
function stageMsg(msg) {
  $("stageMsg").hidden = !msg;
  $("stageMsg").textContent = msg || "";
}
async function doRender() {
  if (!S.preset) return;
  if (!S.csv) { stageMsg("下の「ログ」に CSV を指定してください"); return; }
  if (renderBusy) { renderAgain = true; return; }
  renderBusy = true;
  try {
    const res = await api("POST", "/api/render", {
      preset: S.preset, csv: S.csv, ts: S.ts, sync: S.sync || null, offset: S.offset || 0,
      start: S.start, end: S.end,
      timemap: activeTimemap()?.segments.map(({ v0, v1, offset }) => ({ v0, v1, offset })) ?? null,
    });
    S.skipped = (res.headers.get("X-Skipped-Widgets") || "").split(",").filter(Boolean);
    const url = URL.createObjectURL(await res.blob());
    const img = $("ovImg");
    img.onload = () => { if (lastUrl) URL.revokeObjectURL(lastUrl); lastUrl = url; };
    img.src = url;
    stageMsg(null);
    drawList();
  } catch (e) {
    const errs = (e.info?.errors || []).map((x) => `${x.loc}: ${x.msg}`).join("\n");
    stageMsg(`描画できません: ${e.message}${errs ? "\n" + errs : ""}`);
  } finally {
    renderBusy = false;
    if (renderAgain) { renderAgain = false; doRender(); }
  }
}

let validateTimer;
function scheduleValidate() {
  clearTimeout(validateTimer);
  validateTimer = setTimeout(async () => {
    const r = await api("POST", "/api/validate", { preset: S.preset });
    S.errors = r.errors; S.issues = r.issues;
    drawIssues(); drawList(); drawSvg();
  }, 250);
}

// ---------------------------------------------------------------------
//  背景(動画フレーム)とタイムライン
// ---------------------------------------------------------------------
function tSync() {
  // CLI の --sync と同じ解釈(秒数 or ログ開始日の時刻)。ログ情報が無ければ 0
  const s = (S.sync || "").trim();
  let t = 0;
  if (s && s !== "auto") {
    if (/^-?\d+(\.\d+)?$/.test(s)) t = Number(s);
    else if (S.logInfo) {
      const [hh, mm, ss] = s.split(":").map(Number);
      const st = S.logInfo.start.split(" ")[1].split(":").map(Number);
      t = (hh * 3600 + mm * 60 + (ss || 0)) - (st[0] * 3600 + st[1] * 60 + st[2]);
    }
  }
  return t + (Number(S.offset) || 0);
}
function useVideo() { return !!(S.video && S.videoInfo); }
// OCR の同期は、取得したときの動画欄と今の動画欄が同じで、ログ秒(offset)があるときだけ使う
function activeTimemap() {
  const tm = S.timemap;
  return useVideo() && tm && tm.key === S.video && tm.segments.every((g) => g.offset != null) ? tm : null;
}
function segmentAt(v) {
  const segs = activeTimemap()?.segments || [];
  let i = segs.length - 1;
  while (i > 0 && segs[i].v0 > v) i--;
  return segs.length ? { i, n: segs.length, seg: segs[i] } : null;
}
// 動画秒 → ログ秒(CLI の TimeMap.to_log と同じ。区間の外は端の区間で外挿)
function logAt(v) {
  const g = segmentAt(v);
  return g ? v + g.seg.offset + (Number(S.offset) || 0) : tSync() + v;
}
// スライダーの位置を --start / --end と同じ基準(動画の秒、動画が無ければ --sync からの秒)で返す
function currentPos() { return Math.max(0, Math.round((useVideo() ? S.videoT : S.ts - tSync()) * 10) / 10); }
// 動画欄は 1 行 1 パス(複数行・フォルダはサーバー側でファイル名順に連結される)
const videoList = () => S.video.split("\n").map((s) => s.trim()).filter(Boolean);
const videoQuery = () => videoList().map((v) => `video=${encodeURIComponent(v)}`).join("&");
function videoFileAt(t) {
  const files = S.videoInfo?.files || [];
  let i = files.length - 1;
  while (i > 0 && files[i].start > t) i--;
  return files.length > 1 ? { i, n: files.length, f: files[i], local: t - files[i].start } : null;
}
function syncTimeline() {
  const sl = $("timeSlider");
  if (useVideo()) {
    sl.max = S.videoInfo.duration.toFixed(1);
    sl.value = S.videoT;
    $("timeInput").value = S.videoT.toFixed(1);
    S.ts = logAt(S.videoT);
  } else {
    sl.max = (S.logInfo?.t_end ?? 100).toFixed(1);
    sl.value = S.ts;
    $("timeInput").value = S.ts.toFixed(1);
  }
  const lap = S.logInfo?.laps.filter((l) => l.t <= S.ts).pop();
  const vf = useVideo() && videoFileAt(S.videoT);
  const sg = useVideo() && segmentAt(S.videoT);
  const vfLabel = (vf ? ` (${vf.i + 1}/${vf.n} ${vf.f.path.split("/").pop()} ${vf.local.toFixed(1)} s)` : "") +
    (sg && sg.n > 1 ? ` 区間 ${sg.i + 1}/${sg.n}` : "");
  $("timeLabel").textContent = (useVideo() ? `動画 ${S.videoT.toFixed(1)} s${vfLabel} → ` : "") +
    `ログ ${S.ts.toFixed(1)} s${S.logInfo ? ` / ${S.logInfo.t_end.toFixed(0)} s` : ""}${lap ? `  LAP ${lap.lap}` : ""}`;
}
let frameTimer, frameLoading = false, frameWanted = null;
function updateBackground() {
  const img = $("bgImg");
  if (!useVideo()) {
    img.hidden = true; img.removeAttribute("src"); hideVideo();
    frameLoading = false; frameWanted = null;   // 読み込み中に src を消すと onload が来ないので戻しておく
    return;
  }
  if (videoPlaying) return;            // 再生中は <video> が背景
  clearTimeout(frameTimer);
  frameTimer = setTimeout(() => requestFrame(
    `/api/frame?${videoQuery()}&t=${S.videoT.toFixed(2)}&width=${canvasW()}&height=${canvasH()}`), S.playing ? 0 : 120);
}
// 取り出し中に次を頼まれても src を差し替えない(差し替えると読み込みが打ち切られ、1 枚も出なくなる)。
// 読み終わってから最後に頼まれたものを読む
function requestFrame(url) {
  const img = $("bgImg");
  if (frameLoading) { frameWanted = url; return; }
  frameLoading = true;
  img.onload = img.onerror = () => {
    frameLoading = false;
    img.hidden = false;
    if (!videoPlaying) $("bgVideo").hidden = true;   // 一時停止した位置の静止画が出てから <video> を隠す
    const next = frameWanted;
    frameWanted = null;
    if (next && next !== url) requestFrame(next);
  };
  img.src = url;
}

// 再生中は <video> で元の動画をそのまま流す(1 フレームずつ取り出すのでは間に合わない)。
// 一時停止中は本番と同じ ffmpeg の静止画。ブラウザで再生できない形式ならコマ送りに戻る
let videoPlaying = false, videoFile = -1, videoSrc = "", videoRaf;
function fileAt(t) {
  const files = S.videoInfo.files;
  let i = files.length - 1;
  while (i > 0 && files[i].start > t) i--;
  return { i, local: Math.max(0, t - files[i].start) };
}
async function startVideo() {
  const v = $("bgVideo");
  const { i, local } = fileAt(S.videoT);
  const src = `/api/video-file?${videoQuery()}&i=${i}`;
  if (src !== videoSrc) { v.src = videoSrc = src; }
  videoFile = i;
  v.currentTime = local;
  try {
    await v.play();
  } catch (e) {
    videoSrc = "";
    return false;
  }
  if (!S.playing) { v.pause(); return true; }      // 待っている間に止められた
  videoPlaying = true;
  v.hidden = false;
  $("bgImg").hidden = true;
  cancelAnimationFrame(videoRaf);
  videoRaf = requestAnimationFrame(videoTick);
  return true;
}
function videoTick() {
  if (!videoPlaying) return;
  S.videoT = S.videoInfo.files[videoFile].start + $("bgVideo").currentTime;
  syncTimeline();
  scheduleRender(0);        // 描画中なら終わってから最新の位置で描く(doRender)
  drawCommand();
  videoRaf = requestAnimationFrame(videoTick);
}
function onVideoEnded() {
  if (!videoPlaying) return;
  const files = S.videoInfo.files;
  videoPlaying = false;
  if (videoFile + 1 < files.length) {              // 連結している次のファイルへ
    S.videoT = files[videoFile + 1].start;
    startVideo();
  } else {
    S.videoT = S.videoInfo.duration;
    togglePlay();
  }
}
// 再生中にスライダーを動かしたら <video> をシークする
function seekVideo() {
  const { i, local } = fileAt(S.videoT);
  if (i === videoFile) $("bgVideo").currentTime = local;
  else { videoPlaying = false; startVideo(); }
}
// 再生を止める。止めた位置を S.videoT に残し、静止画に差し替える
function stopVideo() {
  cancelAnimationFrame(videoRaf);
  const v = $("bgVideo");
  if (videoPlaying) S.videoT = S.videoInfo.files[videoFile].start + v.currentTime;
  videoPlaying = false;
  v.pause();
}
function hideVideo() {
  stopVideo();
  $("bgVideo").hidden = true;
}
function setTime(v) {
  if (useVideo()) S.videoT = Math.max(0, Math.min(v, S.videoInfo.duration));
  else S.ts = Math.max(0, Math.min(v, S.logInfo?.t_end ?? v));
  syncTimeline();
  if (videoPlaying) seekVideo();
  else updateBackground();
  scheduleRender(S.playing ? 0 : 30);
  drawCommand();
  saveLocal();
}
async function loadLog() {
  S.csv = $("csvInput").value.trim();
  saveLocal();
  if (!S.csv) { S.logInfo = null; syncTimeline(); doRender(); return; }
  try {
    const d = S.preset?.data || {};
    S.logInfo = await api("GET", `/api/log-info?csv=${encodeURIComponent(S.csv)}&rpm_on_threshold=${d.rpm_on_threshold ?? 300}&heading_lookback_s=${d.heading_lookback_s ?? 3}`);
    status(`ログ: 開始 ${S.logInfo.start} / ${S.logInfo.t_end.toFixed(0)} 秒${S.logInfo.missing?.length ? `(なし: ${S.logInfo.missing.join(", ")})` : ""}`, 5000);
    if (S.ts > S.logInfo.t_end) S.ts = Math.min(150, S.logInfo.t_end);
  } catch (e) {
    S.logInfo = null;
    stageMsg(`ログを読めません: ${e.message}`);
  }
  syncTimeline();
  scheduleRender(0);
  drawCommand();
}
async function autoSync() {
  if (!useVideo()) { status("先に動画を読み込んでください"); return; }
  if (!S.csv) { status("先にログ(CSV)を指定してください"); return; }
  const btn = $("btnAutoSync");
  btn.disabled = true;
  btn.textContent = "解析中…";
  status("焼き込み日時を読み取っています(長い動画では数十秒かかります)", 0);
  try {
    const r = await api("POST", "/api/auto-sync", { video: videoList(), csv: S.csv });
    S.timemap = { key: S.video, segments: r.segments };
    S.sync = "auto";
    $("syncInput").value = "auto";
    const n = r.segments.length;
    status(`自動同期: ${n} 区間` + (r.warnings.length ? `(${r.warnings.join(" / ")})` : ""), r.warnings.length ? 0 : 4000);
    $("syncInput").title = r.segments.map((g, i) =>
      `区間 ${i + 1}: 動画 ${g.v0.toFixed(2)}〜${g.v1.toFixed(2)} s  焼き込み ${g.wall.replace("T", " ")}`).join("\n");
    saveLocal(); syncTimeline(); updateBackground(); scheduleRender(0); drawCommand();
  } catch (e) {
    status(`自動同期できません: ${e.message}`, 0);
  } finally {
    btn.disabled = false;
    btn.textContent = "OCR で自動同期";
  }
}
async function loadVideo() {
  if (S.playing) togglePlay();
  S.video = $("videoInput").value.trim();
  if (S.timemap && S.timemap.key !== S.video) {          // 別の動画の OCR 結果は使わない
    S.timemap = null;
    if (S.sync === "auto") { S.sync = ""; $("syncInput").value = ""; }
  }
  saveLocal();
  if (!S.video) { S.videoInfo = null; $("videoInfo").textContent = ""; syncTimeline(); updateBackground(); return; }
  try {
    S.videoInfo = await api("GET", `/api/video-info?${videoQuery()}`);
    const v = S.videoInfo;
    const n = v.files.length > 1 ? `${v.files.length} 本を連結 / 合計 ` : "";
    $("videoInfo").textContent = `${v.width}×${v.height} ${v.fps.toFixed(2)} fps ${n}${v.duration.toFixed(1)} s`;
    $("videoInfo").title = v.files.map((f) => `${f.start.toFixed(1).padStart(7)} s  ${f.path}`).join("\n");
    S.videoT = Math.min(S.videoT, v.duration);
  } catch (e) {
    S.videoInfo = null;
    $("videoInfo").textContent = `読めません: ${e.message}`;
  }
  syncTimeline();
  updateBackground();
  scheduleRender(0);
  drawCommand();
}
let playTimer, playLast;
async function togglePlay() {
  S.playing = !S.playing;
  $("btnPlay").textContent = S.playing ? "❚❚" : "▶";
  clearInterval(playTimer);
  if (!S.playing) {
    stopVideo();
    syncTimeline(); updateBackground(); scheduleRender(0); drawCommand(); saveLocal();
    return;
  }
  if (useVideo()) {
    if (await startVideo()) return;
    if (!S.playing) return;
    status("この動画はブラウザで再生できないので、静止画のコマ送りで再生します", 5000);
  }
  playLast = performance.now();
  playTimer = setInterval(() => {
    if (renderBusy) return;      // 描画が追いつかなければコマ落ちさせる
    const now = performance.now();
    const dt = (now - playLast) / 1000;
    playLast = now;
    setTime((useVideo() ? S.videoT : S.ts) + dt);
  }, 50);
}

// ---------------------------------------------------------------------
//  左: ウィジェット一覧
// ---------------------------------------------------------------------
function issueLevel(id) {
  const lv = S.issues.filter((i) => i.widgets.includes(id)).map((i) => i.level);
  return lv.includes("error") ? "error" : lv.includes("warning") ? "warning" : null;
}
function drawList() {
  const ul = $("widgetList");
  if (!S.preset) { ul.replaceChildren(); return; }
  $("btnGlobal").classList.toggle("selected", S.selected === null);
  ul.replaceChildren(...S.preset.widgets.map((w) => {
    const lv = issueLevel(w.id);
    const li = el("li", {
      class: [w.id === S.selected && "selected", !w.enabled && "disabled", S.skipped.includes(w.id) && "skipped"].filter(Boolean).join(" "),
      onclick: () => select(w.id),
    },
    el("input", {
      type: "checkbox", checked: w.enabled, title: "表示",
      onclick: (e) => e.stopPropagation(),
      onchange: (e) => edit((p) => { p.widgets.find((x) => x.id === w.id).enabled = e.target.checked; }),
    }),
    el("span", { class: "name" }, w.id),
    el("span", { class: "type" }, S.typeMap[w.type]?.title || w.type),
    lv ? el("span", { class: `mark ${lv}`, title: lv }) : null);
    return li;
  }));
  const has = !!S.selected;
  for (const b of ["btnDup", "btnUp", "btnDown", "btnRemove"]) $(b).disabled = !has;
}
function select(id) {
  S.selected = id;
  drawList(); drawSvg(); drawProps();
}

// ---------------------------------------------------------------------
//  中央: SVG の枠・ハンドル
// ---------------------------------------------------------------------
function svgEl(tag, attrs) {
  const e = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
}
function fitStage() {
  const wrap = $("stageWrap");
  const W = canvasW(), H = canvasH();
  const sc = Math.min((wrap.clientWidth - 24) / W, (wrap.clientHeight - 24) / H);
  $("stage").style.width = `${Math.max(100, W * sc)}px`;
  $("stage").style.height = `${Math.max(56, H * sc)}px`;
  drawSvg();
}
function px() { return canvasW() / ($("stage").clientWidth || canvasW()); }  // 画面 1px あたりのキャンバス px
function drawSvg() {
  const svg = $("svg");
  const W = canvasW(), H = canvasH();
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.replaceChildren();
  const k = px();
  const defs = svgEl("defs", {});
  const pat = svgEl("pattern", { id: "hatch", width: 16, height: 16, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" });
  pat.append(svgEl("rect", { width: 16, height: 16, fill: "rgba(255,60,60,0.06)" }), svgEl("line", { x1: 0, y1: 0, x2: 0, y2: 16, stroke: "rgba(255,80,80,0.35)", "stroke-width": 6 }));
  defs.append(pat);
  svg.append(defs);
  if (!S.preset) return;
  if (S.showKeepout) {
    for (const ko of S.preset.keepout) {
      svg.append(svgEl("rect", { class: "keepout", x: ko.x, y: ko.y, width: ko.w, height: ko.h, "stroke-width": k }));
      const t = svgEl("text", { class: "klabel", x: ko.x + 8 * k, y: ko.y + 18 * k, "font-size": 13 * k });
      t.textContent = `配置禁止: ${ko.name}`;
      svg.append(t);
    }
  }
  for (const w of S.preset.widgets) {
    const sel = w.id === S.selected;
    if (!sel && !S.showBoxes) {
      // 枠を出さなくてもクリックで選べるよう透明な当たり判定は置く
      svg.append(svgEl("rect", { "data-id": w.id, x: w.x, y: w.y, width: w.w, height: w.h, fill: "transparent", style: "cursor:move" }));
      continue;
    }
    if (!w.enabled && !sel && !S.showDisabled) continue;
    const lv = issueLevel(w.id);
    svg.append(svgEl("rect", {
      "data-id": w.id, x: w.x, y: w.y, width: w.w, height: w.h,
      class: ["wbox", sel && "selected", !w.enabled && "disabled", lv && `issue-${lv}`].filter(Boolean).join(" "),
      "stroke-width": (sel ? 2 : 1.5) * k,
    }));
  }
  const w = widget(S.selected);
  if (w) {
    const label = svgEl("text", { class: "label", x: w.x + 2 * k, y: w.y > 20 * k ? w.y - 6 * k : w.y + w.h + 16 * k, "font-size": 13 * k });
    label.textContent = `${w.id}  ${w.x}, ${w.y}  ${w.w}×${w.h}`;
    svg.append(label);
    const hs = 9 * k;
    for (const [h, hx, hy] of handles(w)) {
      svg.append(svgEl("rect", { class: "handle", "data-handle": h, x: hx - hs / 2, y: hy - hs / 2, width: hs, height: hs, "stroke-width": k, style: `cursor:${h}-resize` }));
    }
  }
}
function handles(w) {
  const { x, y } = w, r = w.x + w.w, b = w.y + w.h, cx = x + w.w / 2, cy = y + w.h / 2;
  return [["nw", x, y], ["n", cx, y], ["ne", r, y], ["e", r, cy], ["se", r, b], ["s", cx, b], ["sw", x, b], ["w", x, cy]];
}
function toCanvas(e) {
  const svg = $("svg");
  const pt = svg.createSVGPoint();
  pt.x = e.clientX; pt.y = e.clientY;
  return pt.matrixTransform(svg.getScreenCTM().inverse());
}

let drag = null;
function onPointerDown(e) {
  if (!S.preset || e.button !== 0) return;
  const t = e.target;
  const p = toCanvas(e);
  if (t.dataset.handle && S.selected) {
    const w = widget(S.selected);
    drag = { mode: "resize", h: t.dataset.handle, start: p, orig: { ...w }, before: clone(S.preset) };
  } else if (t.dataset.id) {
    select(t.dataset.id);
    const w = widget(t.dataset.id);
    drag = { mode: "move", start: p, orig: { ...w }, before: clone(S.preset) };
  } else {
    select(null);
    return;
  }
  $("svg").setPointerCapture(e.pointerId);
  e.preventDefault();
}
function onPointerMove(e) {
  const p = toCanvas(e);
  $("coords").textContent = `${Math.round(p.x)}, ${Math.round(p.y)}`;
  if (!drag) return;
  const w = widget(S.selected);
  const o = drag.orig, dx = p.x - drag.start.x, dy = p.y - drag.start.y;
  if (drag.mode === "move") {
    w.x = snap(o.x + dx); w.y = snap(o.y + dy);
  } else {
    let x0 = o.x, y0 = o.y, x1 = o.x + o.w, y1 = o.y + o.h;
    const h = drag.h;
    if (h.includes("w")) x0 = snap(o.x + dx);
    if (h.includes("e")) x1 = snap(x1 + dx);
    if (h.includes("n")) y0 = snap(o.y + dy);
    if (h.includes("s")) y1 = snap(y1 + dy);
    let nw = Math.max(8, x1 - x0), nh = Math.max(8, y1 - y0);
    if (SQUARE_TYPES.has(w.type) || e.shiftKey) {
      // 縦横比を保つ(辺のハンドルならその方向、角なら大きい方に合わせる)
      const ratio = o.w / o.h;
      if (h === "n" || h === "s") nw = Math.round(nh * ratio);
      else if (h === "e" || h === "w") nh = Math.round(nw / ratio);
      else if (nw / ratio > nh) nh = Math.round(nw / ratio); else nw = Math.round(nh * ratio);
    }
    w.w = nw; w.h = nh;
    w.x = h.includes("w") ? x1 - nw : x0;
    w.y = h.includes("n") ? y1 - nh : y0;
  }
  drawSvg();
}
function onPointerUp() {
  if (!drag) return;
  const w = widget(S.selected);
  const o = drag.orig;
  if (w && (w.x !== o.x || w.y !== o.y || w.w !== o.w || w.h !== o.h)) {
    S.history.push(drag.before);
    S.future = [];
    drag = null;
    changed();
  } else {
    drag = null;
  }
}
function nudge(dx, dy) {
  const w = widget(S.selected);
  if (!w) return;
  edit((p) => { const x = p.widgets.find((v) => v.id === w.id); x.x += dx; x.y += dy; });
}

// ---------------------------------------------------------------------
//  右: プロパティ
// ---------------------------------------------------------------------
function drawProps() {
  const box = $("props");
  if (!S.preset || !S.schema) { box.replaceChildren(); return; }
  const w = widget(S.selected);
  if (!w) { box.replaceChildren(...globalProps()); return; }
  const t = S.typeMap[w.type];
  const set = (name) => (v) => edit((p) => {
    const x = p.widgets.find((y) => y.id === w.id);
    if (name === "id") {
      if (!/^[A-Za-z0-9_-]+$/.test(v) || p.widgets.some((y) => y !== x && y.id === v)) { status("id は英数字・_・- で、重複しないこと"); return; }
      S.selected = v;
    }
    x[name] = v;
  });
  const params = Object.fromEntries(t.params.map((p) => [p.name, p]));
  const geom = el("div", { class: "grid4" }, ["x", "y", "w", "h"].map((k) => {
    const inp = el("input", { type: "number", value: w[k], step: 1, min: k === "w" || k === "h" ? 8 : undefined });
    inp.addEventListener("change", () => set(k)(Math.round(Number(inp.value))));
    return el("label", {}, params[k]?.title || k, inp);
  }));
  const own = t.params.filter((p) => !COMMON.includes(p.name));
  return box.replaceChildren(
    el("h2", {}, `${t.title}`),
    el("p", { class: "desc" }, `${t.description}`, el("br"), el("span", { class: "mono" }, `type = "${w.type}"`)),
    el("fieldset", {}, el("legend", {}, "配置"),
      field({ ...params.id, name: "id" }, w.id, set("id")),
      field({ ...params.enabled, name: "enabled" }, w.enabled, set("enabled")),
      geom,
      t.ref_size ? el("div", { class: "help" }, `基準サイズ ${t.ref_size[0]}×${t.ref_size[1]}。文字は min(w/${t.ref_size[0]}, h/${t.ref_size[1]}) 倍で拡縮`) : null,
      el("div", { class: "row" },
        el("button", { class: "small", onclick: () => edit((p) => Object.assign(p.widgets.find((y) => y.id === w.id), { w: t.default_box.w, h: t.default_box.h })) }, "既定のサイズ"),
        el("button", { class: "small", onclick: () => edit((p) => Object.assign(p.widgets.find((y) => y.id === w.id), t.default_box)) }, "既定の位置とサイズ"))),
    own.length ? el("fieldset", {}, el("legend", {}, "パラメータ"), own.map((p) => field(p, w[p.name], set(p.name)))) : null,
  );
}

function globalProps() {
  const p = S.preset, sc = S.schema;
  const top = (name) => ({ name, ...sc.properties[name] });
  const setTop = (name) => (v) => edit((x) => { x[name] = v; });
  const setIn = (sec, name) => (v) => edit((x) => { x[sec][name] = v; });
  const sec = (title, defName, key) => el("fieldset", {}, el("legend", {}, title),
    schemaParams(sc, defName).map((q) => field(q, p[key][q.name], setIn(key, q.name))));
  const ko = el("fieldset", {}, el("legend", {}, "配置禁止帯(lint が重なりを警告)"),
    p.keepout.map((k, i) => {
      const setK = (name) => (v) => edit((x) => { x.keepout[i][name] = v; });
      const nums = el("div", { class: "grid4" }, ["x", "y", "w", "h"].map((n) => {
        const inp = el("input", { type: "number", value: k[n], step: 1 });
        inp.addEventListener("change", () => setK(n)(Math.round(Number(inp.value))));
        return el("label", {}, n, inp);
      }));
      return el("div", { class: "keepout-item" },
        field({ name: "name", title: "名前", type: "string" }, k.name, setK("name")), nums,
        el("div", { class: "row end" }, el("button", { class: "small danger", onclick: () => edit((x) => x.keepout.splice(i, 1)) }, "この帯を削除")));
    }),
    el("div", { class: "row" }, el("button", { class: "small", onclick: () => edit((x) => x.keepout.push({ name: "", x: 0, y: 380, w: canvasW(), h: 260 })) }, "帯を追加")));
  return [
    el("h2", {}, "全体設定"),
    el("p", { class: "desc mono" }, `presets/${S.name}.toml`),
    el("fieldset", {}, el("legend", {}, "プリセット"),
      field(top("name"), p.name, setTop("name")),
      field(top("description"), p.description, setTop("description"))),
    sec("キャンバス(基準解像度)", "Canvas", "canvas"),
    sec("配色・フォント", "Theme", "theme"),
    sec("判定の閾値", "DataParams", "data"),
    ko,
  ];
}

// ---------------------------------------------------------------------
//  下: 検査結果・コマンド
// ---------------------------------------------------------------------
function drawIssues() {
  const ul = $("issues");
  const items = [
    ...S.errors.map((e) => ({ level: "error", msg: `${e.loc}: ${e.msg}`, widgets: [] })),
    ...S.issues,
  ];
  if (!items.length) { ul.replaceChildren(el("li", { class: "ok" }, "問題なし")); return; }
  ul.replaceChildren(...items.map((i) => el("li", {
    class: i.level, onclick: () => i.widgets[0] && select(i.widgets[0]),
  }, el("span", { class: "lv" }, i.level), i.msg)));
}
const q = (s) => (/^[\w./:@-]+$/.test(s) ? s : `'${s.replace(/'/g, "'\\''")}'`);
// ---------------------------------------------------------------------
//  書き出し(サーバーが ecorun-overlay を別プロセスで実行する)
// ---------------------------------------------------------------------
// 出力の候補: 動画と同じフォルダに「名前_overlay.mp4」。動画が無ければ CSV の隣に透過 .mov
function defaultOutput(kind) {
  const src = (useVideo() ? videoList()[0] : S.csv || "overlay").replace(/\/+$/, "");
  const i = src.lastIndexOf("/");
  const dir = i >= 0 ? src.slice(0, i + 1) : "";
  const stem = src.slice(i + 1).replace(/\.[^.]*$/, "");
  if (kind === "concat") return `${dir}${stem}_joined.mp4`;
  return `${dir}${stem}_overlay.${useVideo() ? "mp4" : "mov"}`;
}
async function startExport(kind, overwrite = false) {
  if (kind === "overlay" && !S.csv) { status("先にログ(CSV)を指定してください"); return; }
  const tm = activeTimemap();
  const body = {
    kind, overwrite, output: (kind === "overlay" && S.output) || defaultOutput(kind),
    video: useVideo() ? videoList() : [],
    preset: S.preset, csv: S.csv, offset: Number(S.offset) || 0, start: S.start, end: S.end,
    // OCR 済みならその区間を渡す(CLI で OCR をやり直さない)
    sync: tm ? null : S.sync || null,
    timemap: tm ? tm.segments.map(({ v0, v1, wall }) => ({ v0, v1, wall })) : null,
  };
  if (S.playing) togglePlay();
  try {
    showExport(await api("POST", "/api/export", body));
    pollExport();
  } catch (e) {
    if (e.status === 409 && e.info.exists) {
      if (await dialog(`${body.output} は既にあります。上書きしますか?`, { ok: "上書き" })) startExport(kind, true);
      return;
    }
    status(`書き出せません: ${e.message}`, 0);
  }
}
let exportTimer;
async function pollExport() {
  clearTimeout(exportTimer);
  try {
    const st = await api("GET", "/api/export");
    showExport(st);
    if (st.state === "running") exportTimer = setTimeout(pollExport, 1000);
  } catch {
    exportTimer = setTimeout(pollExport, 3000);
  }
}
function fmtSec(s) {
  s = Math.round(s);
  return s >= 60 ? `${Math.floor(s / 60)} 分 ${String(s % 60).padStart(2, "0")} 秒` : `${s} 秒`;
}
function showExport(st) {
  const running = st.state === "running";
  $("btnExport").disabled = $("btnConcat").disabled = running;
  $("btnCancelExport").hidden = !running;
  const pg = $("exportProgress");
  pg.hidden = !running;
  if (st.total) { pg.max = st.total; pg.value = st.frame; } else pg.removeAttribute("value");   // 準備中・連結は不定
  const last = st.log?.at(-1) || "";
  $("exportStatus").textContent = {
    idle: "",
    running: st.cancelling ? "中止しています…" : st.total
      ? `書き出し中 ${Math.floor(st.frame / st.total * 100)}%(残り約 ${fmtSec(st.eta)})`
      : st.kind === "concat" ? "連結中…" : "準備中…",
    done: `完了(${fmtSec(st.elapsed)}): ${st.output}`,
    failed: `失敗: ${last}`,
    cancelled: "中止しました",
  }[st.state] ?? "";
  $("exportStatus").classList.toggle("error", st.state === "failed");
  // 実行したコマンドと出力はマウスを載せると見える
  $("exportStatus").title = st.command ? `${st.command}\n\n${(st.log || []).join("\n")}` : "";
}

function drawCommand() {
  if (!S.preset) return;
  const csv = q(S.csv || "LOG.CSV");
  const sync = S.sync ? ` --sync ${q(S.sync)}` : "";
  // --sync auto は動画が要るので、プレビューにも --video を付ける
  const previewVideo = S.sync === "auto" && S.video ? ` --video ${videoList().map(q).join(" ")}` : "";
  const off = Number(S.offset) ? ` --offset ${S.offset}` : "";
  const dur = (S.start ? ` --start ${S.start}` : "") + (S.end != null ? ` --end ${S.end}` : "");
  const videos = videoList().map(q).join(" ");
  const lines = [
    "# 静止画プレビュー(枠つき)",
    `uv run ecorun-overlay ${csv} --preset ${S.name}${previewVideo}${sync}${off}${dur} --preview ${S.ts.toFixed(1)} --debug-boxes -o preview.png`,
    S.video ? "# 動画に合成" : "# 透過オーバーレイ(ProRes 4444)",
    S.video
      ? `uv run ecorun-overlay ${csv} --preset ${S.name} --video ${videos}${sync}${off}${dur} -o out.mp4`
      : `uv run ecorun-overlay ${csv} --preset ${S.name}${sync}${off}${dur} -o overlay.mov`,
    ...(S.videoInfo?.files.length > 1
      ? ["# 動画を連結だけする(オーバーレイなし)", `uv run ecorun-overlay --concat-only --video ${videos} -o joined.mp4`]
      : []),
    "# レイアウト検査",
    `uv run ecorun-layout lint ${S.name}`,
  ];
  $("cmd").textContent = lines.join("\n");
  $("outputInput").placeholder = defaultOutput("overlay");
  $("btnConcat").hidden = !(useVideo() && S.videoInfo.files.length > 1);
  $("cmdNote").textContent = isDirty() ? "※ 未保存の変更はコマンドに反映されません" : "";
}

// ---------------------------------------------------------------------
//  初期化
// ---------------------------------------------------------------------
function bind() {
  $("presetSel").addEventListener("change", async (e) => {
    const name = e.target.value;
    if (isDirty() && !(await dialog(`${S.name} の未保存の変更を破棄して ${name} を開きますか?`, { ok: "破棄して開く" }))) {
      e.target.value = S.name; return;
    }
    loadPreset(name);
  });
  $("btnSave").onclick = () => save();
  $("btnSaveAs").onclick = saveAs;
  $("btnRevert").onclick = () => loadPreset(S.name);
  $("btnDeletePreset").onclick = deletePreset;
  $("btnUndo").onclick = undo;
  $("btnRedo").onclick = redo;
  $("btnGlobal").onclick = () => select(null);
  $("btnAdd").onclick = () => {
    const w = newWidget($("addType").value);
    edit((p) => p.widgets.push(w));
    select(w.id);
  };
  $("btnDup").onclick = duplicate;
  $("btnRemove").onclick = removeSelected;
  $("btnUp").onclick = () => move(1);
  $("btnDown").onclick = () => move(-1);
  $("snapSel").onchange = (e) => { S.snap = Number(e.target.value); saveLocal(); };
  $("chkKeepout").onchange = (e) => { S.showKeepout = e.target.checked; saveLocal(); drawSvg(); };
  $("chkBoxes").onchange = (e) => { S.showBoxes = e.target.checked; saveLocal(); drawSvg(); };
  $("chkDisabled").onchange = (e) => { S.showDisabled = e.target.checked; saveLocal(); drawSvg(); };
  $("csvInput").addEventListener("change", loadLog);
  $("syncInput").addEventListener("change", (e) => {
    S.sync = e.target.value.trim();
    if (S.sync !== "auto") { S.timemap = null; e.target.title = ""; }   // 手で書き換えたら OCR の結果は捨てる
    saveLocal(); syncTimeline(); updateBackground(); scheduleRender(0); drawCommand();
  });
  $("btnAutoSync").onclick = autoSync;
  $("offsetInput").addEventListener("change", (e) => { S.offset = Number(e.target.value) || 0; saveLocal(); syncTimeline(); scheduleRender(0); drawCommand(); });
  const setRange = (key, v) => {
    S[key] = v === "" || v == null || !Number.isFinite(Number(v)) ? null : Math.max(0, Number(v));
    $(`${key}Input`).value = S[key] ?? "";
    saveLocal(); scheduleRender(0); drawCommand();
  };
  $("startInput").addEventListener("change", (e) => setRange("start", e.target.value));
  $("endInput").addEventListener("change", (e) => setRange("end", e.target.value));
  $("btnStartHere").onclick = () => setRange("start", currentPos());
  $("btnEndHere").onclick = () => setRange("end", currentPos());
  $("btnVideo").onclick = loadVideo;
  $("videoInput").addEventListener("change", loadVideo);
  $("btnVideoClear").onclick = () => { $("videoInput").value = ""; loadVideo(); };
  $("timeSlider").addEventListener("input", (e) => setTime(Number(e.target.value)));
  $("timeInput").addEventListener("change", (e) => setTime(Number(e.target.value)));
  $("btnPlay").onclick = togglePlay;
  $("bgVideo").addEventListener("ended", onVideoEnded);
  $("outputInput").addEventListener("change", (e) => { S.output = e.target.value.trim(); saveLocal(); });
  $("btnExport").onclick = () => startExport("overlay");
  $("btnConcat").onclick = () => startExport("concat");
  $("btnCancelExport").onclick = async () => showExport(await api("POST", "/api/export/cancel"));
  $("btnCopy").onclick = async () => { await navigator.clipboard.writeText($("cmd").textContent); status("コマンドをコピーしました"); };
  const svg = $("svg");
  svg.addEventListener("pointerdown", onPointerDown);
  svg.addEventListener("pointermove", onPointerMove);
  svg.addEventListener("pointerup", onPointerUp);
  svg.addEventListener("pointercancel", onPointerUp);
  new ResizeObserver(fitStage).observe($("stageWrap"));
  window.addEventListener("beforeunload", (e) => { if (isDirty()) e.preventDefault(); });
  document.addEventListener("keydown", (e) => {
    const mod = e.metaKey || e.ctrlKey;
    if (mod && e.key.toLowerCase() === "s") { e.preventDefault(); save(); return; }
    const typing = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement?.tagName) || $("dlg").open;
    if (typing) return;
    if (mod && e.key.toLowerCase() === "z") { e.preventDefault(); e.shiftKey ? redo() : undo(); return; }
    if (mod && e.key.toLowerCase() === "y") { e.preventDefault(); redo(); return; }
    if (mod && e.key.toLowerCase() === "d") { e.preventDefault(); duplicate(); return; }
    const step = e.shiftKey ? 10 : 1;
    const arrows = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
    if (arrows[e.key] && S.selected) { e.preventDefault(); nudge(...arrows[e.key]); return; }
    if ((e.key === "Delete" || e.key === "Backspace") && S.selected) { e.preventDefault(); removeSelected(); return; }
    if (e.key === "Escape") select(null);
    if (e.key === " ") { e.preventDefault(); togglePlay(); }
  });
}
function duplicate() {
  const w = widget(S.selected);
  if (!w) return;
  const c = { ...clone(w), id: uniqueId(w.id.replace(/_\d+$/, "")), x: w.x + 20, y: w.y + 20 };
  edit((p) => p.widgets.splice(p.widgets.findIndex((x) => x.id === w.id) + 1, 0, c));
  select(c.id);
}
function removeSelected() {
  if (!S.selected) return;
  const id = S.selected;
  edit((p) => { p.widgets = p.widgets.filter((x) => x.id !== id); });
  select(null);
  status(`${id} を削除しました(元に戻す: ⌘/Ctrl+Z)`);
}
function move(dir) {
  const i = S.preset.widgets.findIndex((x) => x.id === S.selected);
  const j = i + dir;
  if (i < 0 || j < 0 || j >= S.preset.widgets.length) return;
  edit((p) => { [p.widgets[i], p.widgets[j]] = [p.widgets[j], p.widgets[i]]; });
}

async function init() {
  bind();
  const local = loadLocal();
  Object.assign(S, {
    csv: local.csv ?? "", output: local.output ?? "", sync: local.sync ?? "", offset: local.offset ?? 0, start: local.start ?? null, end: local.end ?? null, timemap: local.timemap ?? null,
    video: local.video ?? "", snap: local.snap ?? 10, ts: local.ts ?? 150, videoT: local.videoT ?? 0,
    showKeepout: local.showKeepout ?? true, showBoxes: local.showBoxes ?? true, showDisabled: local.showDisabled ?? false,
  });
  $("snapSel").value = String(S.snap);
  $("chkKeepout").checked = S.showKeepout;
  $("chkBoxes").checked = S.showBoxes;
  $("chkDisabled").checked = S.showDisabled;
  $("syncInput").value = S.sync;
  $("offsetInput").value = S.offset;
  $("startInput").value = S.start ?? "";
  $("endInput").value = S.end ?? "";
  $("videoInput").value = S.video;
  $("outputInput").value = S.output;

  const [types, schema, logs] = await Promise.all([api("GET", "/api/widget-types"), api("GET", "/api/schema"), api("GET", "/api/logs")]);
  S.widgetTypes = types;
  S.typeMap = Object.fromEntries(types.map((t) => [t.type, t]));
  S.schema = schema;
  $("addType").replaceChildren(...types.map((t) => el("option", { value: t.type }, `${t.title}(${t.type})`)));
  $("csvList").replaceChildren(...logs.map((f) => el("option", { value: f })));
  if (!S.csv && logs.length) S.csv = logs[logs.length - 1];
  $("csvInput").value = S.csv;

  await refreshPresets();
  const name = S.presets.some((p) => p.name === local.name) ? local.name : (S.presets.find((p) => p.name === "default") || S.presets[0])?.name;
  if (name) await loadPreset(name, { quiet: true });
  fitStage();
  await loadLog();
  if (S.video) await loadVideo();
  watchEvents();
  pollExport();          // 書き出し中に再読込したら進み具合の表示を戻す
}

init().catch((e) => banner(`初期化に失敗しました: ${e.message}`, [], "error"));
