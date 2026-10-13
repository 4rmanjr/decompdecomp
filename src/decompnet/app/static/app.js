"use strict";
/* decomp.wtf GUI. Plain JS, no build step. Everything shown from agents or ROMs is untrusted text:
   it only ever reaches the page through textContent or esc(). */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const hex8 = (n) => "0x" + (n >>> 0).toString(16).toUpperCase().padStart(8, "0");
const clock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
const pctOf = (best, insns) => (best == null ? null : Math.max(0, 1 - best / Math.max(1, insns * 100)));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function api(path, { method = "GET", body, raw } = {}) {
  const opts = { method, headers: {} };
  if (method !== "GET") opts.headers["X-Decomp"] = "1";
  if (raw !== undefined) { opts.body = raw; opts.headers["Content-Type"] = "application/octet-stream"; }
  else if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch { /* not json */ }
  if (!res.ok) throw new Error((data && data.error) || `request failed (${res.status})`);
  return data;
}

function toast(html, kind = "") {
  const t = document.createElement("div");
  t.className = "toast " + kind;
  t.innerHTML = html;
  $("toasts").append(t);
  setTimeout(() => { t.style.opacity = "0"; t.style.transition = "opacity .4s"; }, kind === "err" ? 7000 : 4200);
  setTimeout(() => t.remove(), kind === "err" ? 7500 : 4700);
}

async function followJob(id, logEl) {
  for (;;) {
    const job = await api(`/api/job?id=${id}`);
    if (logEl) { logEl.textContent = job.log.join("\n"); logEl.scrollTop = logEl.scrollHeight; }
    if (job.state === "done") return job.result;
    if (job.state === "error") throw new Error(job.error);
    await sleep(500);
  }
}

/* ------------------------------------------------------------------ theme */
const THEME_KEY = "decomp-wtf-theme";
function applyTheme(t) {
  if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
  colors = null;
  if (S.map.length) drawMap();
}
try { applyTheme(localStorage.getItem(THEME_KEY)); } catch { /* storage off */ }
$("themeBtn").onclick = () => {
  const dark = getComputedStyle(document.documentElement).colorScheme.includes("dark");
  const next = dark ? "light" : "dark";
  try { localStorage.setItem(THEME_KEY, next); } catch { /* storage off */ }
  applyTheme(next);
};

/* ------------------------------------------------------------------ state + routing */
const S = {
  st: null, slug: null, ov: null, map: [], byName: new Map(), e: 0, a: 0, seq: {}, sessions: [],
  scores: new Map(), claims: [], flash: new Map(), timer: null, lastMapFetch: 0, fn: null, recent: [],
};

async function loadState() { S.st = await api("/api/state"); return S.st; }

function route() {
  const m = location.hash.match(/^#\/p\/([\w-]+)/);
  stopPolling();
  closeAll();
  S.slug = null;
  if (m) openProject(m[1]);
  else if (location.hash === "#/catalog") showCatalog();
  else showHome();
}
window.addEventListener("hashchange", route);

function show(view) {
  for (const v of ["home", "catalog", "project"]) $(v).hidden = view !== v;
  window.scrollTo(0, 0);
}

/* ------------------------------------------------------------------ home */
let picked = null;

async function showHome() {
  show("home");
  $("crumb").hidden = true;
  $("workingPill").hidden = true;
  resetDrop();
  const st = await loadState();
  loadCatalog().then((c) => { if (c.length) $("forkCount").textContent = c.length; }).catch(() => {});
  const box = $("projects");
  box.innerHTML = "";
  $("recent").hidden = !st.projects.length;
  for (const p of st.projects) {
    const pct = p.bytes ? (100 * p.matched_bytes) / p.bytes : 0;
    const a = document.createElement("a");
    a.className = "card";
    a.href = `#/p/${p.slug}`;
    const meta = p.kind === "fork"
      ? `${p.origin === "scratch" ? "from scratch" : "fork"} · ${esc(p.platform_name || "?")}${p.version ? " · " + esc(p.version) : ""}${p.ready ? "" : " · needs setup"}`
      : `${esc(p.rom.game_code)} · ${esc(p.rom.region)} · ${esc(p.compiler.name)}`;
    a.innerHTML = `<div><h4>${esc(p.title)}</h4><p class="meta">${meta}</p></div>
      <div class="meter"><i></i></div><p class="meta">${p.matched} / ${p.functions} functions · ${pct.toFixed(1)}% of code</p>`;
    box.append(a);
    requestAnimationFrame(() => { a.querySelector(".meter i").style.width = pct + "%"; });
  }
}

function resetDrop() {
  picked = null;
  $("dropIdle").hidden = false;
  $("dropPicked").hidden = true;
  $("dropBusy").hidden = true;
  $("own").checked = false;
  $("analyse").disabled = true;
  $("file").value = "";
}

const drop = $("drop");
drop.addEventListener("click", (e) => { if (!$("dropIdle").hidden && !e.target.closest("button")) $("file").click(); });
drop.addEventListener("keydown", (e) => { if ((e.key === "Enter" || e.key === " ") && !$("dropIdle").hidden) { e.preventDefault(); $("file").click(); } });
drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); if (e.dataTransfer.files[0]) pickFile(e.dataTransfer.files[0]); });
$("file").addEventListener("change", () => { if ($("file").files[0]) pickFile($("file").files[0]); });
$("own").addEventListener("change", updateAnalyse);
$("cancelPick").onclick = resetDrop;

/* Read the header in the browser so the user sees what they picked before anything happens. */
function parseRom(buf) {
  const b = new Uint8Array(buf);
  const magic = [b[0], b[1], b[2], b[3]].map((x) => x.toString(16).padStart(2, "0")).join("");
  const order = { "80371240": "z64", "37804012": "v64", "40123780": "n64" }[magic];
  if (!order) throw new Error("This doesn't look like an N64 ROM (.z64, .n64 or .v64).");
  const z = new Uint8Array(Math.min(b.length, 0x101000));
  for (let i = 0; i < z.length; i += 4) {
    const w = order === "z64" ? [b[i], b[i + 1], b[i + 2], b[i + 3]] : order === "v64" ? [b[i + 1], b[i], b[i + 3], b[i + 2]] : [b[i + 3], b[i + 2], b[i + 1], b[i]];
    z.set(w, i);
  }
  const text = (from, to) => Array.from(z.slice(from, to)).map((c) => (c >= 32 && c < 127 ? String.fromCharCode(c) : "")).join("").trim();
  const dv = new DataView(z.buffer);
  let jumps = 0, branches = 0;
  for (let i = 0x1000; i + 4 <= z.length; i += 4) {
    const w = dv.getUint32(i);
    if (w >>> 26 === 2) jumps++;
    else if ((w & 0xffff0000) >>> 0 === 0x10000000) branches++;
  }
  const regions = { E: "North America", J: "Japan", P: "Europe", D: "Germany", F: "France", U: "Australia", I: "Italy", S: "Spain", A: "All regions", X: "Europe", Y: "Europe" };
  const code = text(0x3b, 0x3f);
  return {
    title: text(0x20, 0x34) || "Untitled", code, region: regions[code[3]] || "Unknown",
    size: b.length, order, entry: dv.getUint32(8), compiler: branches > jumps ? "IDO" : "KMC",
  };
}

/* What the user dropped, from its first bytes (nothing is uploaded until they confirm). */
const PLATFORM_NAME = { n64: "Nintendo 64", psx: "PlayStation", ps2: "PlayStation 2", playstation: "PlayStation 1 or 2",
  gc: "GameCube", wii: "Wii", gcwii: "GameCube or Wii", gba: "Game Boy Advance" };
const MB = (n) => (n < 1048576 ? `${Math.max(1, Math.round(n / 1024))} KiB` : `${(n / 1048576).toFixed(n < 10485760 ? 1 : 0)} MiB`);

function identifyHead(head, file) {
  const dv = new DataView(head.buffer);
  const text = (a, b) => String.fromCharCode(...head.slice(a, b));
  const ascii = (a, b) => text(a, b).split("\0")[0].replace(/[^\x20-\x7e]/g, "").trim();
  const stem = file.name.replace(/\.[^.]*$/, "");
  const base = { title: stem, size: file.size };
  if (text(0, 8) === "PS-X EXE") return { ...base, platform: "psx", order: "PS-X EXE", entry: dv.getUint32(0x10, true) };
  if (head.length > 0xc0 && head[0xb2] === 0x96 && head[3] === 0xea) {  // GBA: fixed byte, header checksum, ARM branch
    let sum = 0;
    for (let i = 0xa0; i < 0xbd; i++) sum += head[i];
    if (((-(sum + 0x19)) & 0xff) === head[0xbd]) return { ...base, platform: "gba", order: "ROM", code: ascii(0xac, 0xb0), title: ascii(0xa0, 0xac) || stem };
  }
  if (text(0x8001, 0x8006) === "CD001" || text(0x9319, 0x931e) === "CD001") return { ...base, platform: "playstation", order: "disc image" };
  if (text(0, 4) === "\x7fELF" && head[5] === 1 && dv.getUint16(18, true) === 8) return { ...base, platform: "ps2", order: "ELF program", entry: dv.getUint32(24, true) };
  if (head.length > 0x60 && dv.getUint32(0x1c) === 0xc2339f3d) return { ...base, platform: "gc", order: "disc image", code: ascii(0, 6), title: ascii(0x20, 0x80) || stem };
  if (head.length > 0x60 && dv.getUint32(0x18) === 0x5d1c9ea3) return { ...base, platform: "wii", order: "disc image", code: ascii(0, 6), title: ascii(0x20, 0x80) || stem };
  const packed = { "RVZ\x01": "RVZ", "WIA\x01": "WIA", WBFS: "WBFS", CISO: "CISO" }[text(0, 4)] || (dv.getUint32(0, true) === 0xb10bc001 ? "GCZ" : null);
  if (packed) return { ...base, platform: "gcwii", order: `${packed} disc image` };
  if (head.length >= 0x100) {  // a DOL: section offsets, addresses and sizes that make sense, entry in RAM
    const entry = dv.getUint32(0xe0);
    let used = 0, ok = entry >= 0x80000000 && entry < 0x81800000;
    for (let i = 0; i < 18 && ok; i++) {
      const off = dv.getUint32(i * 4), addr = dv.getUint32(72 + i * 4), size = dv.getUint32(144 + i * 4);
      if (!size) continue;
      used++;
      ok = off >= 0x100 && off + size <= file.size && addr >= 0x80000000 && addr < 0x81800000;
    }
    if (ok && used) return { ...base, platform: "gc", order: "program (DOL)", entry };
  }
  return null;
}

async function pickFile(file) {
  const head = new Uint8Array(await file.slice(0, 0x9400).arrayBuffer());
  let info = identifyHead(head, file), buf = file;
  if (!info) {
    if (file.size > 64 * 1024 * 1024) return toast("That isn't a game file decomp.wtf can start from scratch (yet). For other platforms, continue a public decomp below.", "err");
    buf = await file.arrayBuffer();
    try { info = parseRom(buf); info.platform = "n64"; } catch (e) {
      return toast("That isn't a game file decomp.wtf can start from scratch (yet). For other platforms, continue a public decomp below.", "err");
    }
  }
  picked = { buf, info, name: file.name };
  $("romFile").textContent = file.name;
  $("romTitle").textContent = info.title;
  const p = info.platform;
  const facts = p === "n64"
    ? [["Game code", info.code || "—"], ["Region", info.region], ["Size", MB(info.size)], ["Format", info.order],
       ["Entry", hex8(info.entry)], ["Looks like", info.compiler === "IDO" ? "IDO" : "GCC (KMC)"]]
    : [["Platform", PLATFORM_NAME[p]], ["Format", info.order], ["Size", MB(info.size)],
       info.code ? ["Game ID", info.code] : ["Entry", info.entry != null ? hex8(info.entry) : "from the disc"],
       ["Project", ["gc", "wii", "gcwii"].includes(p) ? "dtk-template" : p === "gba" ? "luvdis" : "splat"]];
  $("romFacts").innerHTML = facts.map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`).join("");
  setupCompilers(info);
  $("dropIdle").hidden = true;
  $("dropPicked").hidden = false;
  updateAnalyse();
}

function setupCompilers(info) {
  const p = info.platform, select = $("compilerPick"), note = $("compilerNote");
  const cw = S.st?.codewarrior || { installed: false, gc: [], wii: [] };
  $("compilerRow").hidden = false; note.hidden = true; $("mwConsent").hidden = true; $("mwAccept").checked = false;
  if (p === "n64") fillCompilers(select, info.compiler === "IDO" ? "ido-7.1" : "gcc-2.7.2-kmc", "N64");
  else if (p === "psx") fillCompilers(select, "gcc-2.7.2-psx", "PS1");
  else if (p === "ps2") fillCompilers(select, "ee-gcc-2.96", "PS2");
  else if (p === "gba") fillCompilers(select, "agbcc", "GBA");
  else if (p === "playstation") {
    $("compilerRow").hidden = true; note.hidden = false;
    note.textContent = "The compiler is picked from the disc: PSY-Q-era GCC for PlayStation, EE-GCC for PlayStation 2. You can change it later.";
  } else {
    const group = (label, list, dflt) => `<optgroup label="${label}">${list.map((v) => `<option value="${esc(v)}" ${v === dflt ? "selected" : ""}>CodeWarrior ${esc(v)}</option>`).join("")}</optgroup>`;
    select.innerHTML = (p !== "wii" ? group("GameCube", cw.gc, "GC/1.3.2") : "") + (p !== "gc" ? group("Wii", cw.wii, p === "wii" ? "Wii/1.0" : "") : "");
    note.hidden = false;
    note.textContent = "Not sure? Keep the default; you can change it later in configure.py if functions don't match.";
    $("mwConsent").hidden = cw.installed;
  }
}

function updateAnalyse() {
  const needsConsent = !$("mwConsent").hidden;
  $("analyse").disabled = !$("own").checked || (needsConsent && !$("mwAccept").checked);
}
$("mwAccept").addEventListener("change", updateAnalyse);

function fillCompilers(select, current, platform) {
  const tcs = (S.st?.toolchains || []).filter((t) => t.kind === "compiler" && (!platform || !t.platform || t.platform === platform));
  select.innerHTML = tcs.map((t) => {
    const note = !t.supported ? " (not available on this computer)" : t.installed ? "" : t.license === "proprietary" ? " (install in Setup)" : " (will be installed)";
    return `<option value="${esc(t.id)}" ${t.id === current ? "selected" : ""} ${t.supported ? "" : "disabled"}>${esc(t.name)} · ${esc(t.use)}${note}</option>`;
  }).join("");
}

async function runImport(query, file, title, name = "") {
  $("dropIdle").hidden = true;
  $("dropPicked").hidden = true;
  $("dropBusy").hidden = false;
  $("busyTitle").textContent = title;
  $("jobLog").textContent = "";
  $("impUp").hidden = !(file instanceof Blob);
  try {
    let started;
    if (file instanceof Blob) {
      started = await uploadFile(`/api/import?${query}&name=${encodeURIComponent(name || file.name || "game.bin")}`, file,
        (p) => { $("impUp").firstElementChild.style.width = (p * 100).toFixed(1) + "%"; $("busyTitle").textContent = p < 1 ? `Copying ${title.replace(/^\w+ /, "")} into decomp.wtf… ${(p * 100).toFixed(0)}%` : title; });
    } else {
      started = await api(`/api/import?${query}`, { method: "POST", raw: file ?? new ArrayBuffer(0) });
    }
    $("impUp").hidden = true;
    const res = await followJob(started.job, $("jobLog"));
    location.hash = `#/p/${res.slug}`;
  } catch (e) {
    toast(esc(e.message), "err");
    resetDrop();
  }
}

async function ensureCodeWarrior() {
  if ($("mwConsent").hidden) return;
  $("busyTitle").textContent = "Installing CodeWarrior…";
  const { job } = await api("/api/toolchain", { method: "POST", body: { id: "mwcc", accept_license: true } });
  await followJob(job, $("jobLog"));
  await loadState();
}

$("analyse").onclick = async () => {
  if (!picked || $("analyse").disabled) return;
  const p = picked.info.platform;
  const tc = $("compilerRow").hidden ? "" : $("compilerPick").value;
  const title = `Analysing ${picked.info.title}…`;
  if (!$("mwConsent").hidden) {
    $("dropPicked").hidden = true; $("dropBusy").hidden = false; $("impUp").hidden = true;
    try { await ensureCodeWarrior(); } catch (e) { toast(esc(e.message), "err"); resetDrop(); return; }
  }
  const body = picked.buf instanceof Blob ? picked.buf : new Blob([picked.buf]);
  runImport(`confirmed=1${tc ? `&toolchain=${encodeURIComponent(tc)}` : ""}`, body, title, picked.name);
};
$("pathPick").onclick = () => {
  const body = dialog("Use a file on this computer", `<p class="muted">For big disc images: decomp.wtf reads the file where it is instead of copying it through the browser.</p>
    <label class="field"><span>Path to the game file</span><input id="ppPath" class="mono" placeholder="/home/me/games/my-game.iso"></label>
    <label class="check"><input type="checkbox" id="ppOwn"><span>This is my own copy of this game, obtained legally. The results stay with me for my own use.</span></label>
    <div class="row"><button class="primary" id="ppGo" type="button" disabled>Start decompiling</button><button class="ghost" data-close type="button">Cancel</button></div>`);
  body.querySelector("#ppOwn").onchange = () => { $("ppGo").disabled = !$("ppOwn").checked; };
  body.querySelector("#ppGo").onclick = () => {
    const path = $("ppPath").value.trim();
    if (!path) return;
    $("dialog").hidden = true;
    runImport(`confirmed=1&path=${encodeURIComponent(path)}`, undefined, "Analysing your game…");
  };
};
$("demoBtn").onclick = () => runImport("demo=1", undefined, "Opening Demo Quest 64…");

/* ------------------------------------------------------------------ project */
async function openProject(slug) {
  show("project");
  S.slug = slug; S.e = 0; S.a = 0; S.seq = {}; S.scores = new Map(); S.flash = new Map(); S.sessions = []; S.claims = [];
  $("activity").innerHTML = ""; $("thoughts").innerHTML = "";
  try {
    if (!S.st) await loadState();
    S.ov = await api(`/api/project?slug=${slug}`);
  } catch (e) {
    toast(esc(e.message), "err");
    location.hash = "#/";
    return;
  }
  $("crumb").hidden = false;
  $("crumb").innerHTML = `<b>${esc(S.ov.title)}</b>${S.ov.rom.game_code ? `<span class="code">${esc(S.ov.rom.game_code)}</span>` : ""}`;
  const fork = S.ov.kind === "fork";
  $("mapTitle").textContent = fork ? "Function map" : "ROM map";
  $("mapHint").textContent = fork ? "every square is a function, grouped by source file" : "every square is a function, in ROM order";
  for (const id of ["exportBtn", "compilerBtn", "inviteBtn"]) $(id).hidden = fork;
  $("forkSetup").hidden = true;
  $("grid").hidden = false;
  $("stats").hidden = fork && !S.ov.ready;
  renderStats(S.ov);
  if (fork && !S.ov.ready) { renderForkSetup(); return; }
  await refreshMap();
  await poll(true);
  S.timer = setInterval(() => poll(false), 1000);
}

function stopPolling() { if (S.timer) clearInterval(S.timer); S.timer = null; }

async function refreshMap() {
  S.map = await api(`/api/map?slug=${S.slug}`);
  S.byName = new Map(S.map.map((f, i) => [f.name, i]));
  S.lastMapFetch = Date.now();
  layoutMap();
}

let polling = false;
async function poll(first) {
  if (polling || !S.slug) return;
  polling = true;
  try {
    const live = await api(`/api/live?slug=${S.slug}&e=${S.e}&a=${S.a}&s=${encodeURIComponent(JSON.stringify(S.seq))}`);
    S.ov = live.overview;
    renderStats(live.overview);
    let mapDirty = false;
    for (const ev of live.events) {
      S.e = Math.max(S.e, ev.id);
      if (["match", "split", "rename"].includes(ev.kind)) mapDirty = true;
      if (ev.kind === "match" && !first) {
        S.flash.set(ev.detail, performance.now());
        toast(`<code>${esc(ev.detail)}</code>&nbsp;matched by ${esc(ev.agent || "an agent")}`, "match");
      }
    }
    for (const at of live.attempts) {
      S.a = Math.max(S.a, at.id);
      if (!S.scores.has(at.function)) S.scores.set(at.function, []);
      S.scores.get(at.function).push({ score: at.score, t: at.created_at, insns: at.n_insns });
    }
    addActivity(live.events, live.attempts, first);
    S.claims = live.claims;
    S.sessions = live.sessions;
    for (const s of live.sessions) {
      if (s.feed.length) S.seq[s.id] = s.feed[s.feed.length - 1].seq;
    }
    addThoughts(live.sessions, first);
    renderSessions(live.sessions);
    renderNow(live.claims);
    if (mapDirty || Date.now() - S.lastMapFetch > 8000) await refreshMap();
    else applyClaims();
    if (S.fn && !$("drawer").hidden) refreshFn(false);
  } catch (e) {
    if (String(e.message).includes("Failed to fetch")) $("feedHint").textContent = "app not reachable";
  } finally {
    polling = false;
  }
}

function renderStats(ov) {
  const pct = ov.bytes ? (100 * ov.matched_bytes) / ov.bytes : 0;
  $("pct").textContent = pct < 10 ? pct.toFixed(2) : pct.toFixed(1);
  $("pctBar").style.width = pct + "%";
  $("fnCount").textContent = `${ov.matched} / ${ov.functions}`;
  $("fnSub").textContent = `${ov.functions - ov.matched} to go`;
  $("compiles").textContent = ov.attempts.toLocaleString();
  const recent = [...S.scores.values()].flat().filter((x) => x.t > Date.now() / 1000 - 600).length;
  $("rate").textContent = recent ? `${recent} in the last 10 minutes` : "each one checked against your ROM";
  renderRoom(ov);
  if (ov.kind === "fork") { renderFork(ov); return; }
  $("forkNotice").hidden = true;
  $("compilerLabel").textContent = "Compiler";
  $("compilerName").textContent = ov.compiler.name;
  $("compilerSub").textContent = ov.compiler.flags.join(" ");
  const n = $("compilerNotice");
  if (!ov.compiler.supported) {
    n.hidden = false; n.className = "notice bad";
    n.innerHTML = `<span><b>${esc(ov.compiler.name)}</b> has no build for this computer yet. Pick another compiler in Compiler settings.</span>`;
  } else if (!ov.compiler.installed) {
    n.hidden = false; n.className = "notice";
    n.innerHTML = ov.compiler.proprietary
      ? `<span><b>${esc(ov.compiler.name)}</b> isn't installed. It's proprietary software (SGI's compiler, recompiled by the decomp community); install it only if you accept that.</span><button class="primary small-btn" id="tcInstall" type="button">Install…</button>`
      : `<span><b>${esc(ov.compiler.name)}</b> isn't installed yet.</span><button class="primary small-btn" id="tcInstall" type="button">Install now</button>`;
    $("tcInstall").onclick = () => installToolchain(ov.compiler.toolchain, ov.compiler.proprietary);
  } else n.hidden = true;
}

function renderRoom(ov) {
  const n = $("roomNotice");
  const room = ov.room;
  $("inviteBtn").hidden = (room && room.role === "guest") || ov.kind === "fork";
  if (!room) { n.hidden = true; return; }
  n.hidden = false;
  const people = (room.members || []).map((m) => `<span class="member">${esc(m.name)}${m.matched ? ` · ${m.matched}` : ""}</span>`).join("");
  if (room.role === "host") {
    n.className = "notice room";
    n.innerHTML = `<span><b>Sharing this project.</b> ${room.members.length ? "In the room:" : "Nobody has joined yet."}</span><span class="members">${people}</span><span class="grow"></span><button class="ghost small-btn" id="roomManage" type="button">Invite link</button>`;
    $("roomManage").onclick = openInvite;
  } else {
    const others = (room.members || []).filter((m) => m.name !== room.member).map((m) => `<span class="member">${esc(m.name)}</span>`).join("");
    n.className = "notice room" + (room.connected ? "" : " off");
    n.innerHTML = `<span><b>${room.connected ? "In a friend's room" : "Room offline"}</b> as ${esc(room.member)}. ${room.connected ? "Your agents share their work with the host" + (others ? " and" : ".") : esc(room.error || "Waiting for the host…")}</span><span class="members">${room.connected ? others : ""}</span><span class="grow"></span><button class="ghost small-btn" id="roomReconnect" type="button">${room.connected ? "New link?" : "Reconnect"}</button><button class="ghost small-btn" id="roomLeave" type="button">Leave room</button>`;
    $("roomReconnect").onclick = openReconnect;
    $("roomLeave").onclick = async () => {
      if (!confirm("Leave this room? Your local copy of the project stays; it stops syncing.")) return;
      await api("/api/collab/leave", { method: "POST", body: { slug: S.slug } }).catch((e) => toast(esc(e.message), "err"));
    };
  }
}

let inviteTimer = null;
async function openInvite() {
  const info = await api(`/api/collab?slug=${S.slug}`).catch((e) => { toast(esc(e.message), "err"); return null; });
  if (!info) return;
  const body = dialog("Invite friends", `<p>Friends join this project from their own decomp.wtf. Each of them needs <b>their own copy of exactly this game</b>: the app checks that their ROM is identical to yours.</p>
    <p class="muted small">Only the work is shared: which function each agent is on, the C attempts, matches, names, structs and notes. No ROM data or assembly is ever sent; everyone's agents read the code from their own copy. Every match is checked against your ROM before it counts. Keep the room private: the decompiled code of a commercial game belongs to its rights holder.</p>
    <div id="inviteBody"></div>`);
  const render = (inf) => {
    const box = body.querySelector("#inviteBody");
    if (!box) return;
    if (!inf.sharing) {
      box.innerHTML = `<div class="row"><button class="primary" id="startShare" type="button" ${inf.tunnel_supported ? "" : "disabled"}>Start sharing</button>
        <span class="muted small">Opens a private tunnel to this computer while the app runs (Cloudflare quick tunnel, free, no account).</span></div><pre class="joblog" id="shareLog" hidden></pre>`;
      box.querySelector("#startShare").onclick = async () => {
        box.querySelector("#shareLog").hidden = false;
        box.querySelector("#startShare").disabled = true;
        try {
          const { job } = await api("/api/collab/start", { method: "POST", body: { slug: S.slug } });
          await followJob(job, box.querySelector("#shareLog"));
          render(await api(`/api/collab?slug=${S.slug}`));
        } catch (e) { toast(esc(e.message), "err"); box.querySelector("#startShare").disabled = false; }
      };
      return;
    }
    const people = (inf.room?.members || []).map((m) => `<div class="line-item"><div class="txt"><b>${esc(m.name)}</b><span>${m.agents.length ? esc(m.agents.join(", ")) : "no agents yet"} · ${m.matched} matched</span></div></div>`).join("");
    box.innerHTML = `<label class="field"><span>Invite link (send it to friends who own this game)</span></label>${cmdBox(inf.link || "")}
      <div class="setup-group"><h3>In the room</h3>${people || `<p class="muted small">Nobody yet. The link works while decomp.wtf is open on this computer.</p>`}</div>
      <div class="row"><button class="ghost danger" id="stopShare" type="button">Stop sharing</button></div>`;
    wireCopies(box);
    box.querySelector("#stopShare").onclick = async () => {
      await api("/api/collab/stop", { method: "POST", body: { slug: S.slug } });
      render(await api(`/api/collab?slug=${S.slug}`));
    };
  };
  render(info);
  clearInterval(inviteTimer);
  inviteTimer = setInterval(async () => {
    if ($("dialog").hidden) { clearInterval(inviteTimer); return; }
    const inf = await api(`/api/collab?slug=${S.slug}`).catch(() => null);
    if (inf && inf.sharing) render(inf);
  }, 4000);
}

function openReconnect() {
  const body = dialog("Reconnect to the room", `<p class="muted">When the host restarts decomp.wtf, their invite link changes. Paste the new one; you stay the same member and keep everything you've done.</p>
    <label class="field"><span>New invite link</span><input id="rLink" class="mono" placeholder="https://….trycloudflare.com/join/…"></label>
    <div class="row"><button class="primary" id="rGo" type="button">Reconnect</button></div><pre class="joblog" id="rLog" hidden></pre>`);
  body.querySelector("#rGo").onclick = async () => {
    if (!$("rLink").value.trim()) return toast("Paste the host's new invite link.", "err");
    $("rLog").hidden = false;
    $("rGo").disabled = true;
    try {
      const { job } = await api("/api/collab/reconnect", { method: "POST", body: { slug: S.slug, link: $("rLink").value.trim() } });
      await followJob(job, $("rLog"));
      $("dialog").hidden = true;
      toast("Back in the room.", "match");
    } catch (e) { toast(esc(e.message), "err"); $("rGo").disabled = false; }
  };
}

function openJoin() {
  const body = dialog("Join a friend's room", `<p class="muted">Paste the invite link your friend sent you, then pick <b>your own copy</b> of the same game. decomp.wtf checks it's identical to theirs; your ROM itself is never sent.</p>
    <label class="field"><span>Invite link</span><input id="jLink" class="mono" placeholder="https://….trycloudflare.com/join/…"></label>
    <label class="field"><span>Your name in the room</span><input id="jName" maxlength="24" placeholder="e.g. alex"></label>
    <label class="field"><span>Your copy of the game</span><input id="jRom" type="file" accept=".z64,.n64,.v64,.rom,.bin"></label>
    <label class="check"><input type="checkbox" id="jOwn"><span>This is my own copy of this game, obtained legally. I'll keep what the room makes private.</span></label>
    <div class="row"><button class="primary" id="jGo" type="button">Join</button></div><pre class="joblog" id="jLog" hidden></pre>`);
  body.querySelector("#jGo").onclick = async () => {
    const file = $("jRom").files[0];
    if (!$("jLink").value.trim() || !/^[A-Za-z0-9_-]{1,24}$/.test($("jName").value) || !file || !$("jOwn").checked) {
      return toast("Fill in the link, a name (letters, digits, - or _), your ROM, and confirm it's your own copy.", "err");
    }
    $("jLog").hidden = false;
    $("jGo").disabled = true;
    try {
      const q = `link=${encodeURIComponent($("jLink").value.trim())}&name=${encodeURIComponent($("jName").value)}&confirmed=1`;
      const { job } = await api(`/api/collab/join?${q}`, { method: "POST", raw: await file.arrayBuffer() });
      const res = await followJob(job, $("jLog"));
      $("dialog").hidden = true;
      location.hash = `#/p/${res.slug}`;
    } catch (e) { toast(esc(e.message), "err"); $("jGo").disabled = false; }
  };
}

async function installToolchain(id, proprietary) {
  if (proprietary && !confirm("This downloads SGI's IDO compiler (recompiled for modern systems by the decompals project). It is proprietary software: install it only if you accept that risk. Continue?")) return;
  toast("Installing the compiler…");
  try {
    const { job } = await api("/api/toolchain", { method: "POST", body: { id, accept_license: proprietary } });
    await followJob(job);
    toast("Compiler installed.", "match");
    await loadState();
    if (S.slug) renderStats(await api(`/api/project?slug=${S.slug}`));
    if (!$("setupSheet").hidden) openSetup();
  } catch (e) { toast(esc(e.message), "err"); }
}

/* ------------------------------------------------------------------ forks */
const PLAT_ORDER = ["gc", "wii", "ps", "ps2", "n64", "gba", "nds", "switch", "win32", "xbox360", "3ds", "xbox", "psp"];
const PLAT_SHORT = { gc: "GameCube", wii: "Wii", ps: "PS1", ps2: "PS2", n64: "N64", gba: "GBA", nds: "DS", switch: "Switch", win32: "PC", xbox360: "Xbox 360", "3ds": "3DS", xbox: "Xbox", psp: "PSP" };
const cat = { list: null, plat: "", q: "", done: false };

async function loadCatalog() {
  if (!cat.list) cat.list = (await api("/api/catalog")).projects;
  return cat.list;
}

function ago(iso) {
  if (!iso) return "";
  const d = (Date.now() - Date.parse(iso)) / 86400000;
  return d < 1 ? "today" : d < 2 ? "yesterday" : d < 60 ? `${Math.round(d)} days ago` : d < 730 ? `${Math.round(d / 30)} months ago` : `${Math.round(d / 365)} years ago`;
}

async function showCatalog() {
  show("catalog");
  $("crumb").hidden = true;
  $("workingPill").hidden = true;
  $("catGrid").innerHTML = `<p class="muted">Loading projects from decomp.dev…</p>`;
  try { await loadCatalog(); } catch (e) { $("catGrid").innerHTML = `<p class="muted">Couldn't reach decomp.dev (${esc(e.message)}). You can still fork a repository by its address below.</p>`; return; }
  if (!S.st) await loadState().catch(() => {});
  renderCatalog();
}

function renderCatalog() {
  const all = cat.list || [];
  const counts = {};
  const shown = all.filter((p) => cat.done || p.matched < 99.99);
  for (const p of shown) counts[p.platform] = (counts[p.platform] || 0) + 1;
  const plats = PLAT_ORDER.filter((k) => counts[k]).concat(Object.keys(counts).filter((k) => !PLAT_ORDER.includes(k)));
  $("catChips").innerHTML = [`<button class="chip ${cat.plat ? "" : "on"}" data-plat="" type="button">All <span>${shown.length}</span></button>`]
    .concat(plats.map((k) => `<button class="chip ${cat.plat === k ? "on" : ""}" data-plat="${esc(k)}" type="button">${esc(PLAT_SHORT[k] || k.toUpperCase())} <span>${counts[k]}</span></button>`)).join("");
  $("catChips").querySelectorAll(".chip").forEach((b) => { b.onclick = () => { cat.plat = b.dataset.plat; renderCatalog(); }; });
  const q = cat.q.toLowerCase();
  const forked = new Map((S.st?.projects || []).filter((p) => p.kind === "fork").map((p) => [p.repo_url, p.slug]));
  const rows = all.filter((p) => (!cat.plat || p.platform === cat.plat) && (cat.done || p.matched < 99.99)
    && (!q || `${p.name} ${p.id}`.toLowerCase().includes(q)));
  $("catNote").textContent = `${rows.length} of ${all.length} projects${cat.done ? "" : " (finished ones hidden)"}. Progress from decomp.dev; decomp.wtf isn't affiliated with these projects.`;
  $("catGrid").innerHTML = rows.slice(0, 240).map((p) => `<button class="cat-card" type="button" data-id="${esc(p.id)}">
      <div class="cc-top"><span class="plat">${esc(PLAT_SHORT[p.platform] || p.platform_name)}</span>${forked.has(p.repo_url) ? `<span class="badge matched">forked</span>` : p.matched >= 99.99 ? `<span class="badge">done</span>` : ""}</div>
      <h4>${esc(p.name)}</h4>
      <p class="meta">${esc(p.id)}${p.version ? " · " + esc(p.version) : ""}</p>
      <div class="meter"><i data-w="${p.matched}"></i></div>
      <p class="meta"><b>${p.matched.toFixed(1)}%</b> of code · ${p.functions ? `${p.matched_functions.toLocaleString()} / ${p.functions.toLocaleString()} functions` : "functions unknown"}${p.updated ? " · " + ago(p.updated) : ""}</p>
    </button>`).join("") || `<p class="muted">Nothing matches.</p>`;
  requestAnimationFrame(() => $("catGrid").querySelectorAll(".meter i").forEach((i) => { i.style.width = i.dataset.w + "%"; }));
  $("catGrid").querySelectorAll(".cat-card").forEach((b) => {
    b.onclick = () => {
      const p = all.find((x) => x.id === b.dataset.id);
      if (forked.has(p.repo_url)) location.hash = `#/p/${forked.get(p.repo_url)}`;
      else openForkDialog(p);
    };
  });
}
$("catSearch").addEventListener("input", () => { cat.q = $("catSearch").value; renderCatalog(); });
$("catDone").addEventListener("change", () => { cat.done = $("catDone").checked; renderCatalog(); });
$("repoFork").onclick = () => {
  const url = $("repoUrl").value.trim();
  if (!url) return;
  openForkDialog({ name: url.replace(/\/+$/, "").split("/").pop().replace(/\.git$/, ""), repo_url: url, platform: "", platform_name: "", id: url });
};

function openForkDialog(p) {
  const body = dialog(`Fork ${p.name}`, `<p>decomp.wtf clones <a class="link" href="${esc(p.repo_url)}" target="_blank" rel="noopener">${esc(p.id)}</a> to this computer and sets it up for your agents.</p>
    <dl class="facts">${[["Platform", p.platform_name || "?"], ["Version", p.version || "the project's default"], ["Progress", p.functions ? `${p.matched.toFixed(1)}% of code` : "?"]]
      .map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl>
    <p class="muted small">Next you add <b>your own copy of the game</b>. Its code stays on your computer; your agent's provider sees the functions it works on. Setting up runs the project's own build scripts, as building it yourself would, so fork projects you trust. Matches are committed to a local <code>decomp-wtf</code> branch: what you push upstream is up to you and the project's rules.</p>
    <div class="row"><button class="primary" id="forkGo" type="button">Fork it</button><button class="ghost" data-close type="button">Cancel</button></div>
    <pre class="joblog" id="forkLog" hidden></pre>`);
  body.querySelector("#forkGo").onclick = async () => {
    $("forkGo").disabled = true;
    $("forkLog").hidden = false;
    try {
      const { job } = await api("/api/fork", { method: "POST", body: { repo: p.repo_url, name: p.name, platform: p.platform, version: p.version } });
      const r = await followJob(job, $("forkLog"));
      $("dialog").hidden = true;
      await loadState();
      location.hash = `#/p/${r.slug}`;
    } catch (e) { toast(esc(e.message), "err"); $("forkGo").disabled = false; }
  };
}

function renderFork(ov) {
  $("compilerNotice").hidden = true;
  $("compilerLabel").textContent = "Platform";
  $("compilerName").textContent = ov.platform_name || "Fork";
  $("compilerSub").textContent = ov.version || "";
  if (ov.ready && ov.functions === 0) $("rate").textContent = "";
  else if (ov.ready && !ov.attempts) $("rate").textContent = "each one judged by objdiff";
  const n = $("forkNotice");
  if (!ov.ready) { n.hidden = true; return; }
  n.hidden = false;
  const repo = (ov.repo_url || "").replace(/^https?:\/\//, "");
  n.innerHTML = ov.origin === "scratch"
    ? `<span><b>Started from scratch</b> · a ${ov.platform === "gc" || ov.platform === "wii" ? "dtk-template" : "decomp"} project that rebuilds your game byte for byte on every build${ov.compiler_name ? ` (CodeWarrior ${esc(ov.compiler_name)})` : ""}. Matches are committed to git in the project folder.</span><span class="grow"></span><button class="ghost small-btn" id="forkMeasure" type="button">Re-measure</button>`
    : `<span><b>Fork of ${esc(repo)}</b> · matches are committed to the <code>decomp-wtf</code> branch in the project folder.</span><span class="grow"></span><button class="ghost small-btn" id="forkMeasure" type="button">Re-measure</button><a class="ghost small-btn as-btn" href="${esc(ov.repo_url)}" target="_blank" rel="noopener">Repository</a>`;
  $("forkMeasure").onclick = async () => {
    toast("Measuring the whole project with objdiff…");
    try { const { job } = await api("/api/fork/check", { method: "POST", body: { slug: S.slug } }); await followJob(job); toast("Progress updated.", "match"); refreshMap(); }
    catch (e) { toast(esc(e.message), "err"); }
  };
}

function uploadFile(url, file, onProgress) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open("POST", url);
    x.setRequestHeader("X-Decomp", "1");
    x.setRequestHeader("Content-Type", "application/octet-stream");
    x.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
    x.onload = () => {
      let data = null;
      try { data = JSON.parse(x.responseText); } catch { /* not json */ }
      if (x.status >= 200 && x.status < 300) resolve(data); else reject(new Error((data && data.error) || `upload failed (${x.status})`));
    };
    x.onerror = () => reject(new Error("upload failed"));
    x.send(file);
  });
}

async function renderForkSetup() {
  const box = $("forkSetup");
  $("grid").hidden = true;
  box.hidden = false;
  box.innerHTML = `<p class="muted">Looking at the project…</p>`;
  let n;
  try { n = await api(`/api/fork/needs?slug=${S.slug}`); } catch (e) { box.innerHTML = `<p class="muted">${esc(e.message)}</p>`; return; }
  const kinds = { manifest: "decomp.wtf manifest", dtk: "dtk-template (configure.py + ninja)", configure: "configure.py + ninja", splat: "splat + make", objdiff: "objdiff project", custom: "its own setup" };
  const facts = `<dl class="facts">${[["Repository", (S.ov.repo_url || "").replace(/^https?:\/\//, "")], ["Layout", kinds[n.kind] || n.kind], ["Version", n.version || "—"], ["Game goes to", n.path || "see the README"]]
    .map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl>
    <div class="row"><button class="ghost small-btn" id="fsFolder" type="button">Open project folder</button></div>`;
  const run = async (start) => {
    $("fsLog").hidden = false;
    box.querySelectorAll("button, input").forEach((b) => { b.disabled = true; });
    try {
      const { job } = await start();
      await followJob(job, $("fsLog"));
      toast("The project is ready for your agents.", "match");
      openProject(S.slug);
    } catch (e) {
      toast(esc(e.message), "err");
      box.querySelectorAll("button, input").forEach((b) => { b.disabled = false; });
      if ($("fsOwn")) $("fsGo").disabled = !$("fsOwn").checked;
    }
  };
  if (n.auto) {
    box.innerHTML = `<div class="fs-main">
        <p class="eyebrow">Last step</p>
        <h2>Add your copy of the game</h2>
        <p class="muted">${esc(n.what)}. ${esc(n.help || "")}</p>
        <label class="check"><input type="checkbox" id="fsOwn"><span>This is my own copy of this game, obtained legally (for example, dumped from my console). The results stay with me for my own use.</span></label>
        <div class="row"><input type="file" id="fsFile" hidden><button class="primary" id="fsGo" type="button" disabled>Choose the game file…</button>
          <span class="muted small">or</span><input id="fsPath" class="mono grow" placeholder="paste its path on this computer"><button class="ghost" id="fsPathGo" type="button">Use path</button></div>
        <div class="upbar" id="fsUp" hidden><i></i></div>
        <pre class="joblog tall" id="fsLog" hidden></pre>
        <p class="muted small">Then decomp.wtf runs the project's setup and build, which can download the project's own tools and take a few minutes. Put the game in place yourself? <button class="link-btn" id="fsBuild" type="button">Just build</button></p>
      </div><aside class="fs-side">${facts}</aside>`;
    $("fsOwn").onchange = () => { $("fsGo").disabled = !$("fsOwn").checked; };
    $("fsGo").onclick = () => $("fsFile").click();
    $("fsFile").onchange = () => {
      const f = $("fsFile").files[0];
      if (!f) return;
      $("fsUp").hidden = false;
      run(() => uploadFile(`/api/fork/game?slug=${encodeURIComponent(S.slug)}&confirmed=1&name=${encodeURIComponent(f.name)}`, f,
        (p) => { $("fsUp").firstElementChild.style.width = (p * 100).toFixed(1) + "%"; }));
    };
    $("fsPathGo").onclick = () => {
      if (!$("fsOwn").checked) return toast("Confirm that this is your own copy of the game first.", "err");
      if (!$("fsPath").value.trim()) return;
      run(() => api("/api/fork/setup", { method: "POST", body: { slug: S.slug, path: $("fsPath").value.trim(), confirmed: true } }));
    };
    $("fsBuild").onclick = () => run(() => api("/api/fork/setup", { method: "POST", body: { slug: S.slug } }));
  } else {
    box.innerHTML = `<div class="fs-main">
        <p class="eyebrow">Last step</p>
        <h2>Set it up the project's way</h2>
        <p class="muted">${esc(n.help)}</p>
        <ol class="steps"><li>Open a terminal in the project folder.</li><li>Follow the README: it says where your copy of the game goes and how to build.</li><li>Come back and click <b>Check again</b>.</li></ol>
        ${cmdBox(`cd "${S.ov.folder}"`)}
        ${n.readme ? `<p class="col-title">From the project's README</p><pre class="readme" id="fsReadme"></pre>` : ""}
        <div class="row"><button class="primary" id="fsCheck" type="button">Check again</button></div>
        <pre class="joblog tall" id="fsLog" hidden></pre>
      </div><aside class="fs-side">${facts}</aside>`;
    if (n.readme) $("fsReadme").textContent = n.readme;
    wireCopies(box);
    $("fsCheck").onclick = () => run(() => api("/api/fork/check", { method: "POST", body: { slug: S.slug } }));
  }
  $("fsFolder").onclick = () => api("/api/open", { method: "POST", body: { slug: S.slug } }).catch((e) => toast(esc(e.message), "err"));
}

/* ------------------------------------------------------------------ map */
let colors = null, geo = null, animating = false;

function cssColor(name) {
  const probe = document.createElement("i");
  probe.style.color = `var(${name})`;
  document.body.append(probe);
  const rgb = getComputedStyle(probe).color.match(/\d+(\.\d+)?/g).slice(0, 3).map(Number);
  probe.remove();
  return rgb;
}
function readColors() {
  colors = { open: cssColor("--open"), tried: cssColor("--tried"), work: cssColor("--work"), match: cssColor("--match"), hi: cssColor("--match-hi"), ink: cssColor("--ink") };
}
const mix = (a, b, t) => a.map((v, i) => Math.round(v + (b[i] - v) * t));
const rgb = (c, a = 1) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;

function layoutMap() {
  const wrap = $("mapWrap"), canvas = $("map");
  const w = Math.max(200, wrap.clientWidth - 32), n = Math.max(1, S.map.length);
  let size = Math.floor(Math.sqrt((w * 300) / n));
  size = Math.max(5, Math.min(26, size));
  const gap = size >= 12 ? 3 : 2;
  const cols = Math.max(1, Math.floor((w + gap) / (size + gap)));
  const rows = Math.ceil(n / cols);
  const h = rows * (size + gap) - gap;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
  canvas.style.height = h + "px";
  geo = { size, gap, cols, w, h, dpr };
  applyClaims();
}

function applyClaims() {
  const working = new Map(S.claims.map((c) => [c.name, c.agent]));
  for (const f of S.map) {
    if (f.status !== "matched") f.status = working.has(f.name) ? "working" : "open";
    f.agent = working.get(f.name) || null;
    const sc = S.scores.get(f.name);
    if (sc) {
      const best = Math.min(...sc.filter((x) => x.score != null).map((x) => x.score), f.best ?? Infinity);
      if (Number.isFinite(best)) f.best = best;
    }
  }
  drawMap();
}

function drawMap() {
  if (!geo) return;
  if (!colors) readColors();
  const ctx = $("map").getContext("2d");
  const { size, gap, cols, dpr } = geo;
  const now = performance.now();
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, geo.w, geo.h);
  const r = size >= 10 ? 2.5 : size >= 7 ? 1.5 : 1;
  let live = false;
  S.map.forEach((f, i) => {
    const x = (i % cols) * (size + gap), y = Math.floor(i / cols) * (size + gap);
    let c, a = 1;
    if (f.status === "matched") c = colors.match;
    else if (f.status === "working") { c = colors.work; a = 0.55 + 0.45 * Math.sin(now / 260 + i); live = true; }
    else if (f.best != null) c = mix(colors.tried, colors.hi, Math.pow(pctOf(f.best, f.insns) || 0, 3));
    else c = colors.open;
    ctx.fillStyle = rgb(c, a);
    ctx.beginPath(); ctx.roundRect(x, y, size, size, r); ctx.fill();
    const fl = S.flash.get(f.name);
    if (fl) {
      const t = (now - fl) / 1600;
      if (t < 1) {
        live = true;
        const g = size * (0.6 + 1.8 * t);
        ctx.strokeStyle = rgb(colors.match, 1 - t);
        ctx.lineWidth = 2;
        ctx.beginPath(); ctx.roundRect(x - g / 2 + size / 2 - size / 2, y - g / 2 + size / 2 - size / 2, size + g - size + size, size + g - size + size, r + 2); ctx.stroke();
      } else S.flash.delete(f.name);
    }
    if (S.fn === f.name) { ctx.strokeStyle = rgb(colors.ink, 0.9); ctx.lineWidth = 1.5; ctx.strokeRect(x - 1.5, y - 1.5, size + 3, size + 3); }
  });
  if (live && !animating) { animating = true; requestAnimationFrame(() => { animating = false; drawMap(); }); }
}

function mapIndexAt(ev) {
  if (!geo) return -1;
  const rect = $("map").getBoundingClientRect();
  const x = ev.clientX - rect.left, y = ev.clientY - rect.top;
  const col = Math.floor(x / (geo.size + geo.gap)), row = Math.floor(y / (geo.size + geo.gap));
  if (col < 0 || col >= geo.cols || x % (geo.size + geo.gap) > geo.size || y % (geo.size + geo.gap) > geo.size) return -1;
  const i = row * geo.cols + col;
  return i < S.map.length ? i : -1;
}
$("map").addEventListener("mousemove", (ev) => {
  const i = mapIndexAt(ev), tip = $("tip");
  if (i < 0) { tip.hidden = true; return; }
  const f = S.map[i];
  const p = pctOf(f.best, f.insns);
  const status = f.status === "matched" ? "matched" : f.status === "working" ? `${esc(f.agent)} is on it` : f.best != null ? `best ${(p * 100).toFixed(0)}%` : "not started";
  tip.innerHTML = `<b>${esc(f.name)}</b><br>${hex8(f.vram)} · ${f.insns} instructions<br>${status}`;
  tip.hidden = false;
  const wrap = $("mapWrap").getBoundingClientRect();
  let left = ev.clientX - wrap.left + 14, top = ev.clientY - wrap.top + 14;
  if (left + 240 > wrap.width) left -= 260;
  tip.style.left = left + "px"; tip.style.top = top + "px";
});
$("map").addEventListener("mouseleave", () => { $("tip").hidden = true; });
$("map").addEventListener("click", (ev) => { const i = mapIndexAt(ev); if (i >= 0) openFn(S.map[i].name); });
let resizeT = null;
window.addEventListener("resize", () => { clearTimeout(resizeT); resizeT = setTimeout(() => { if (S.map.length) layoutMap(); }, 120); });
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { colors = null; drawMap(); });

/* ------------------------------------------------------------------ now working */
function spark(points, w = 240, h = 34) {
  const vals = points.filter((p) => p.score != null);
  if (!vals.length) return `<svg viewBox="0 0 ${w} ${h}"></svg>`;
  const max = Math.max(...vals.map((p) => p.score), 1);
  const step = vals.length > 1 ? w / (vals.length - 1) : 0;
  const pts = vals.map((p, i) => [i * step, 3 + (h - 6) * (p.score / max)]);
  const d = pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join("");
  const last = pts[pts.length - 1];
  const done = vals[vals.length - 1].score === 0;
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><path d="${d}" fill="none" stroke="var(${done ? "--match" : "--work"})" stroke-width="1.6" vector-effect="non-scaling-stroke"/>
    <circle cx="${last[0]}" cy="${last[1]}" r="2.6" fill="var(${done ? "--match" : "--work"})"/></svg>`;
}

function renderNow(claims) {
  const box = $("now");
  if (!claims.length) { box.innerHTML = ""; return; }
  box.innerHTML = claims.map((c) => {
    const sc = S.scores.get(c.name) || [];
    const p = pctOf(c.best_score, c.n_insns);
    return `<div class="job" data-fn="${esc(c.name)}"><div class="job-top"><span class="job-name">${esc(c.name)}</span><span class="job-who">${esc(c.agent)}</span></div>
      ${spark(sc)}<div class="job-foot"><span>${c.n_insns} insns · ${c.attempts} tries</span><b>${p == null ? "starting" : (p * 100).toFixed(0) + "%"}</b></div></div>`;
  }).join("");
  box.querySelectorAll(".job").forEach((j) => { j.onclick = () => openFn(j.dataset.fn); });
}

/* ------------------------------------------------------------------ sessions */
const STATE_TEXT = { starting: "starting…", working: "working", waiting: "next session in a moment", stopped: "stopped", error: "stopped with an error" };

function renderSessions(sessions) {
  const box = $("sessions");
  $("noSessions").hidden = sessions.length > 0;
  const active = sessions.filter((s) => ["starting", "working", "waiting"].includes(s.state)).length;
  $("workingPill").hidden = !active;
  $("workingText").textContent = `${active} agent${active === 1 ? "" : "s"} working`;
  box.innerHTML = sessions.map((s) => {
    const claim = S.claims.find((c) => c.agent === s.agent);
    const dot = s.state === "error" ? "err" : s.state === "working" ? "on" : s.state === "stopped" ? "" : "work";
    const running = ["starting", "working", "waiting"].includes(s.state);
    return `<div class="session"><div class="session-top"><span class="dot ${dot}"></span><b>${esc(s.label)}</b>
        <button class="ghost" data-stop="${esc(s.id)}" data-remove="${running ? "" : "1"}" type="button">${running ? "Stop" : "Remove"}</button></div>
      <span class="state ${s.state === "error" ? "err" : ""}">${s.state === "error" ? esc(s.error) : esc(STATE_TEXT[s.state] || s.state)}${s.model ? " · " + esc(s.model) : ""}${s.runs ? ` · session ${s.runs}` : ""}</span>
      ${claim ? `<span class="last">on ${esc(claim.name)}</span>` : ""}</div>`;
  }).join("");
  box.querySelectorAll("[data-stop]").forEach((b) => {
    b.onclick = async () => {
      await api("/api/agents/stop", { method: "POST", body: { id: b.dataset.stop, remove: !!b.dataset.remove } }).catch((e) => toast(esc(e.message), "err"));
      poll(false);
    };
  });
}

/* ------------------------------------------------------------------ feeds */
function fnLink(name) { return `<span class="fn" data-fn="${esc(name)}">${esc(name)}</span>`; }
function eventHtml(ev) {
  const who = `<span class="who">${esc(ev.agent || "")}</span>`;
  const d = ev.detail || "";
  switch (ev.kind) {
    case "claim": return `${who} started ${fnLink(d)}`;
    case "match": return `<span class="ok">✓</span> ${who} matched ${fnLink(d)}`;
    case "release": return `${who} set ${fnLink(d)} aside, with notes for the next agent`;
    case "expired": return `${fnLink(d)} was left unfinished`;
    case "context": return `${who} updated context.h: ${esc(d)}`;
    case "rename": return `${who} renamed ${esc(d)}`;
    case "split": return `${who} split ${esc(d)}`;
    case "import": return `ROM analysed: ${esc(d)}`;
    case "compiler": return `compiler set to ${esc(d)}`;
    case "collab": return `${esc(d.replace(" joined", ""))} joined the room`;
    default: return `${who} ${esc(ev.kind)} ${esc(d)}`;
  }
}
function attemptHtml(at) {
  const who = `<span class="who">${esc(at.agent || "")}</span>`;
  if (at.score == null) return `${who} ${fnLink(at.function)} <span class="bad">didn't compile</span>`;
  if (at.score === 0) return `${who} ${fnLink(at.function)} <span class="ok">score 0: byte-identical</span>`;
  const p = pctOf(at.score, at.n_insns);
  return `${who} compiled ${fnLink(at.function)} <span class="score">score ${at.score} · ${(p * 100).toFixed(0)}%</span>`;
}

function addActivity(events, attempts, first) {
  const items = [...events.map((e) => ({ t: e.created_at, html: eventHtml(e) })), ...attempts.map((a) => ({ t: a.created_at, html: attemptHtml(a) }))];
  items.sort((a, b) => a.t - b.t);
  const list = $("activity");
  for (const it of items) {
    const li = document.createElement("li");
    if (!first) li.className = "fresh";
    li.innerHTML = `<time>${clock(it.t)}</time><span>${it.html}</span>`;
    list.prepend(li);
  }
  while (list.children.length > 300) list.lastChild.remove();
  list.querySelectorAll(".fn").forEach((x) => { x.onclick = () => openFn(x.dataset.fn); });
  if (!list.children.length) list.innerHTML = `<li><time></time><span class="muted">Nothing yet. Add an agent and its work shows up here.</span></li>`;
}

/* Agents write Markdown. Escape everything first, then allow only **bold** and `code`. */
function miniMarkdown(text) {
  return esc(text)
    .replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>")
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/^#{1,4} (.*)$/gm, "<b>$1</b>")
    .replace(/^- /gm, "• ");
}

function addThoughts(sessions, first) {
  const list = $("thoughts");
  const items = [];
  for (const s of sessions) for (const f of s.feed) items.push({ ...f, label: s.label });
  items.sort((a, b) => a.t - b.t);
  for (const it of items) {
    const li = document.createElement("li");
    if (!first) li.className = "fresh";
    const time = document.createElement("time"); time.textContent = clock(it.t);
    const src = document.createElement("span"); src.className = "src"; src.textContent = it.label;
    const body = document.createElement("span"); body.className = "t-" + it.type;
    if (it.type === "text") body.innerHTML = miniMarkdown(it.text.slice(0, 1600));
    else body.textContent = it.type === "think" ? it.text.slice(0, 1200) : it.text;
    li.append(time, src, body);
    list.prepend(li);
  }
  while (list.children.length > 400) list.lastChild.remove();
}

document.querySelectorAll(".tab").forEach((t) => {
  t.onclick = () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("on", x === t));
    $("activity").hidden = t.dataset.tab !== "activity";
    $("thoughts").hidden = t.dataset.tab !== "thoughts";
  };
});

/* ------------------------------------------------------------------ inspector */
function highlightAsm(text) {
  return text.split("\n").map((line) => {
    if (/^\s*(glabel|endlabel)/.test(line)) return `<span class="cm">${esc(line)}</span>`;
    if (/^\.L[0-9A-F]+:/.test(line)) return `<span class="lbl">${esc(line)}</span>`;
    const m = line.match(/^(\/\* [^*]+ \*\/)(\s+)(\S+)(.*)$/);
    if (!m) return esc(line);
    const addr = m[1].split(" ")[2] || "";
    return `<span class="addr">${esc(addr)}</span>  <span class="op">${esc(m[3].padEnd(8))}</span> ${esc(m[4].trimStart()).replace(/(\.L[0-9A-F]{8})/g, '<span class="lbl">$1</span>')}`;
  }).join("\n");
}
function highlightC(text) {
  const kw = /\b(void|int|char|short|long|float|double|unsigned|signed|struct|union|enum|typedef|static|extern|const|volatile|return|if|else|for|while|do|switch|case|default|break|continue|goto|sizeof|s8|u8|s16|u16|s32|u32|s64|u64|f32|f64)\b/g;
  return esc(text).split("\n").map((line) => {
    if (/^\s*(\/\*|\*|\/\/)/.test(line)) return `<span class="cm">${line}</span>`;
    return line.replace(kw, '<span class="kw">$1</span>').replace(/\b(0x[0-9A-Fa-f]+|\d+)\b/g, '<span class="num">$1</span>');
  }).join("\n");
}

async function openFn(name) {
  S.fn = name;
  $("drawer").hidden = false;
  drawMap();
  await refreshFn(true);
}
let fnBusy = false;
async function refreshFn(first) {
  if (fnBusy || !S.fn) return;
  fnBusy = true;
  try {
    const d = await api(`/api/function?slug=${S.slug}&name=${encodeURIComponent(S.fn)}`);
    if (!first && d.status !== "matched" && !S.claims.some((c) => c.name === d.name)) return;
    $("fnAddr").textContent = S.ov.kind === "fork"
      ? `${d.unit} · ${d.insns} instructions${d.match_percent != null ? ` · ${d.match_percent.toFixed(1)}% now` : ""}`
      : `${hex8(d.vram)} · ROM 0x${d.rom_offset.toString(16).toUpperCase()} · ${d.insns} instructions`;
    $("fnName").textContent = d.name;
    const working = S.claims.find((c) => c.name === d.name);
    const st = $("fnStatus");
    st.className = "badge " + (d.status === "matched" ? "matched" : working ? "working" : "");
    st.textContent = d.status === "matched" ? "matched" : working ? `${working.agent} working` : d.best != null ? `best ${(pctOf(d.best, d.insns) * 100).toFixed(0)}%` : "not started";
    $("fnAsm").innerHTML = highlightAsm(d.asm);
    const kinds = { matched: "Matching C", best: "Best attempt so far", latest: S.ov.kind === "fork" ? "In the source now" : "Latest attempt", draft: "Machine draft (m2c)" };
    $("fnSrcKind").textContent = kinds[d.source_kind] || "No C yet";
    $("fnSrc").innerHTML = d.source ? highlightC(d.source) : `<span class="cm">/* no attempt yet */</span>`;
    $("copySrc").onclick = () => navigator.clipboard.writeText(d.source || "").then(() => toast("Copied."));
    const sc = d.scores.map((s) => ({ score: s.score }));
    $("fnSpark").parentElement.hidden = !d.scores.length;
    $("fnSpark").innerHTML = spark(sc, 600, 70).replace(/^<svg[^>]*>|<\/svg>$/g, "");
    $("fnSparkNote").textContent = d.scores.length ? `${d.scores.length} compiles · score per compile, lower is better, 0 = byte-identical` : "no compiles yet";
    const xr = [];
    if (d.calls.length) xr.push(`<div><b>calls</b>${d.calls.map(fnLink).join("")}</div>`);
    if (d.called_by.length) xr.push(`<div><b>called by</b>${d.called_by.map(fnLink).join("")}</div>`);
    $("fnXref").innerHTML = xr.join("");
    $("fnXref").querySelectorAll(".fn").forEach((x) => { x.onclick = () => openFn(x.dataset.fn.split("+")[0]); });
    $("fnNotes").innerHTML = d.notes.length ? `<p class="col-title">Notes from agents</p>` + d.notes.map((n) => `<div class="note"><small>${esc(n.agent)} · ${clock(n.created_at)}</small>${esc(n.text)}</div>`).join("") : "";
  } catch (e) {
    if (first) toast(esc(e.message), "err");
  } finally { fnBusy = false; }
}

/* ------------------------------------------------------------------ sheets */
function closeAll() {
  for (const id of ["drawer", "agentSheet", "setupSheet", "dialog"]) $(id).hidden = true;
  S.fn = null;
  if (S.map.length) drawMap();
}
document.querySelectorAll(".drawer").forEach((d) => {
  d.addEventListener("click", (e) => { if (e.target === d || e.target.closest("[data-close]")) { d.hidden = true; if (d.id === "drawer") { S.fn = null; drawMap(); } } });
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeAll(); });

function cmdBox(cmd) {
  return `<div class="cmd"><code>${esc(cmd)}</code><button type="button" data-copy="${esc(cmd)}">Copy</button></div>`;
}
function wireCopies(root) {
  root.querySelectorAll("[data-copy]").forEach((b) => { b.onclick = () => navigator.clipboard.writeText(b.dataset.copy).then(() => { b.textContent = "Copied"; setTimeout(() => (b.textContent = "Copy"), 1500); }); });
}

/* add agent */
let chosenKind = null;
$("addAgent").onclick = openAgentSheet;
async function openAgentSheet() {
  $("agentSheet").hidden = false;
  await loadState();
  const box = $("kinds");
  box.innerHTML = S.st.agents.map((a) => `<button class="kind ${a.id === chosenKind ? "on" : ""}" data-kind="${esc(a.id)}" type="button">
      <b>${esc(a.name)}</b><span>${esc(a.vendor)}</span>
      <em class="${a.installed ? "ok" : "no"}">${a.id === "api" ? "built in" : a.installed ? "installed" : "not installed"}</em></button>`).join("");
  box.querySelectorAll(".kind").forEach((b) => { b.onclick = () => { chosenKind = b.dataset.kind; openAgentSheet(); }; });
  renderKind();
}

function renderKind() {
  const box = $("kindDetail");
  const a = S.st.agents.find((x) => x.id === chosenKind);
  if (!a) { box.innerHTML = `<p class="muted small">Pick the agent you use. You can run several at once; each takes a different function.</p>`; return; }
  if (!a.installed) {
    box.innerHTML = `<div class="detail"><p><b>${esc(a.name)}</b> isn't installed on this computer.</p>
      ${a.needs ? `<p class="muted small">Needs ${esc(a.needs)} first.</p>` : ""}
      ${a.install ? cmdBox(a.install) : ""}
      <div class="row"><button class="primary" id="doInstall" type="button" ${a.needs ? "disabled" : ""}>Install it for me</button><a class="link small" href="${esc(a.docs)}" target="_blank" rel="noopener">About ${esc(a.name)}</a></div>
      <p class="muted small">${esc(a.signin)}</p><pre class="joblog" id="installLog" hidden></pre></div>`;
    wireCopies(box);
    $("doInstall").onclick = async () => {
      $("installLog").hidden = false;
      $("doInstall").disabled = true;
      try {
        const { job } = await api("/api/agents/install", { method: "POST", body: { kind: a.id } });
        await followJob(job, $("installLog"));
        toast(`${esc(a.name)} installed.`, "match");
        openAgentSheet();
      } catch (e) { toast(esc(e.message), "err"); $("doInstall").disabled = false; }
    };
    return;
  }
  const api_ = a.id === "api";
  const set = S.st.settings.api || {};
  const presets = S.st.api_presets;
  const prov = set.provider || "deepseek";
  box.innerHTML = `<div class="detail">
    ${api_ ? `<p>Runs a model through its API with a built-in agent loop. Your key is stored only on this computer.</p>
      <label class="field"><span>Provider</span><select id="apiProv">${Object.entries(presets).map(([k, p]) => `<option value="${k}" ${k === prov ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select></label>
      <label class="field"><span>API address</span><input id="apiBase" class="mono" value="${esc(set.base_url || presets[prov].base_url)}"></label>
      <label class="field"><span>Model</span><input id="apiModel" class="mono" value="${esc(set.model || presets[prov].model)}"></label>
      <label class="field"><span>API key</span><input id="apiKey" type="password" placeholder="${set.has_key ? esc(set.api_key) : "paste your key"}" autocomplete="off"></label>`
    : `<p><b>${esc(a.name)}</b> ${a.version ? `<span class="muted">(${esc(a.version)})</span>` : ""} runs in the background with access to the decomp tools only. ${esc(a.signin)}</p>
      <label class="field"><span>Model (optional)</span><input id="agentModel" class="mono" placeholder="the agent's default"></label>`}
    <div class="row"><span class="muted small grow">How many in parallel</span><span class="stepper"><button type="button" id="minus">−</button><output id="count">1</output><button type="button" id="plus">+</button></span></div>
    <button class="primary" id="startAgent" type="button">Start ${esc(a.name)}</button></div>`;
  let count = 1;
  $("minus").onclick = () => { count = Math.max(1, count - 1); $("count").textContent = count; };
  $("plus").onclick = () => { count = Math.min(6, count + 1); $("count").textContent = count; };
  if (api_) {
    $("apiProv").onchange = () => { const p = presets[$("apiProv").value]; $("apiBase").value = p.base_url; $("apiModel").value = p.model; };
  }
  $("startAgent").onclick = async () => {
    const options = {};
    try {
      if (api_) {
        await api("/api/settings", { method: "POST", body: { api: { provider: $("apiProv").value, base_url: $("apiBase").value, model: $("apiModel").value, api_key: $("apiKey").value } } });
        Object.assign(options, { provider: $("apiProv").value, base_url: $("apiBase").value, model: $("apiModel").value });
      } else if ($("agentModel").value.trim()) options.model = $("agentModel").value.trim();
      await api("/api/agents/start", { method: "POST", body: { kind: a.id, slug: S.slug, count, options } });
      $("agentSheet").hidden = true;
      toast(`${esc(a.name)} is starting.`);
      document.querySelector('.tab[data-tab="thoughts"]').click();
      poll(false);
    } catch (e) { toast(esc(e.message), "err"); }
  };
}

/* setup */
$("setupBtn").onclick = openSetup;
async function openSetup() {
  $("setupSheet").hidden = false;
  const st = await loadState();
  const tcs = st.toolchains.map((t) => `<div class="line-item"><div class="txt"><b>${esc(t.name)}</b><span>${esc(t.use)}${t.license === "proprietary" ? " · proprietary" : ""}</span></div>
      ${!t.supported ? `<span class="muted small">not for this computer</span>` : t.installed ? `<span class="ok-mark">installed</span>` : `<button class="ghost small-btn" data-tc="${esc(t.id)}" data-prop="${t.license === "proprietary" ? 1 : ""}" type="button">Install</button>`}</div>`).join("");
  const ags = st.agents.filter((a) => a.id !== "api").map((a) => `<div class="line-item"><div class="txt"><b>${esc(a.name)}</b><span>${a.installed ? esc(a.version || "installed") : esc(a.install)}</span></div>
      ${a.installed ? `<span class="ok-mark">ready</span>` : `<span class="muted small">not installed</span>`}</div>`).join("");
  $("setupBody").innerHTML = `
    <div class="setup-group"><h3>Compilers and tools</h3>${tcs}</div>
    <div class="setup-group"><h3>AI agents on this computer</h3>${ags}<p class="muted small">Models with an API key (DeepSeek, OpenRouter, Ollama…) need no install: choose "API key" when adding an agent.</p></div>
    <div class="setup-group"><h3>Work rhythm</h3>
      <label class="field"><span>Functions per agent session (a fresh session keeps the agent's context small)</span><input id="perSession" type="number" min="1" max="20" value="${st.settings.per_session}"></label></div>
    <div class="setup-group"><h3>About</h3><p class="muted small">decomp.wtf ${esc(st.version)} on ${esc(st.platform)}. Projects live in <code>${esc(st.home)}</code>. Nothing is sent to us. Open source (MIT).</p></div>`;
  $("setupBody").querySelectorAll("[data-tc]").forEach((b) => { b.onclick = () => installToolchain(b.dataset.tc, !!b.dataset.prop); });
  $("perSession").onchange = () => api("/api/settings", { method: "POST", body: { per_session: Number($("perSession").value) } }).catch(() => {});
}

/* dialogs */
function dialog(title, html) {
  $("dialogTitle").textContent = title;
  $("dialogBody").innerHTML = html;
  $("dialog").hidden = false;
  wireCopies($("dialogBody"));
  return $("dialogBody");
}

$("inviteBtn").onclick = openInvite;
$("joinBtn").onclick = openJoin;
$("exportBtn").onclick = () => {
  const body = dialog("Export a rebuildable project", `<p>decomp.wtf writes a complete decompilation project: the matched functions as C, everything else as assembly, a linker script and <code>build.py</code>. Then it builds it to prove it rebuilds your game <b>byte for byte</b>.</p>
    <p class="muted small">It's the base for ports and mods: keep working on it with any tool or agent. Game code and data stay out of git (they're regenerated from your copy), so the tree is safe to put in a repository.</p>
    <div class="row"><button class="primary" id="expGo" type="button">Export and build</button><button class="ghost" data-close type="button">Cancel</button></div>
    <pre class="joblog" id="expLog" hidden></pre><div id="expResult"></div>`);
  body.querySelector("#expGo").onclick = async () => {
    $("expGo").disabled = true;
    $("expLog").hidden = false;
    try {
      const { job } = await api("/api/export", { method: "POST", body: { slug: S.slug } });
      const r = await followJob(job, $("expLog"));
      $("expResult").innerHTML = r.identical === false
        ? `<div class="notice bad"><span><b>Exported, but the rebuild differs.</b> ${esc(r.log.split("\n").filter(Boolean).pop() || "")}</span></div>`
        : `<div class="notice room"><span><b>Rebuilds byte for byte.</b> ${r.matched != null ? `${r.matched} of ${r.functions} functions are C.` : ""}</span><span class="grow"></span><button class="ghost small-btn" id="expOpen" type="button">Open folder</button></div>
           ${cmdBox(`cd "${r.path}" && python build.py`)}`;
      wireCopies($("expResult"));
      const open = $("expOpen");
      if (open) open.onclick = () => api("/api/open", { method: "POST", body: { slug: S.slug } }).catch(() => {});
    } catch (e) { toast(esc(e.message), "err"); $("expGo").disabled = false; }
  };
};
$("folderBtn").onclick = () => api("/api/open", { method: "POST", body: { slug: S.slug } }).catch((e) => toast(esc(e.message), "err"));
$("compilerBtn").onclick = async () => {
  await loadState();
  const body = dialog("Compiler settings", `<p class="muted">The compiler and flags every function is built with. If almost nothing matches, the game was probably built with another compiler or flags.</p>
    <label class="field"><span>Compiler</span><select id="dlgTc"></select></label>
    <label class="field"><span>Flags</span><input id="dlgFlags" class="mono" value="${esc(S.ov.compiler.flags.join(" "))}"></label>
    <div class="row"><button class="primary" id="dlgSave" type="button">Save</button><button class="ghost" id="dlgReset" type="button">Default flags</button></div>`);
  fillCompilers($("dlgTc"), S.ov.compiler.toolchain, { n64: "N64", psx: "PS1" }[S.ov.platform]);
  $("dlgReset").onclick = () => { $("dlgFlags").value = ""; };
  body.querySelector("#dlgSave").onclick = async () => {
    try {
      const flags = $("dlgFlags").value.trim();
      S.ov = await api("/api/compiler", { method: "POST", body: { slug: S.slug, toolchain: $("dlgTc").value, flags: flags ? flags.split(/\s+/) : null } });
      renderStats(S.ov);
      $("dialog").hidden = true;
      toast("Compiler settings saved.");
    } catch (e) { toast(esc(e.message), "err"); }
  };
};
$("connectBtn").onclick = async () => {
  try {
    const r = await api("/api/agents/manual", { method: "POST", body: { slug: S.slug } });
    dialog("Connect another agent", `<p class="muted">Any MCP-capable agent (Cursor, Claude Desktop, Windsurf, your own) can join this project while decomp.wtf runs. Give it these settings, then ask it to "decompile functions with the decomp tools".</p>
      <label class="field"><span>Claude Code</span></label>${cmdBox(r.claude)}
      <label class="field"><span>MCP config (JSON)</span></label><pre class="code">${esc(r.json)}</pre>
      <p class="muted small">This token only works on this computer, for this project.</p>`);
  } catch (e) { toast(esc(e.message), "err"); }
};
$("deleteBtn").onclick = () => {
  dialog("Remove project", `<p>Remove <b>${esc(S.ov.title)}</b> from decomp.wtf? Matched functions and notes are deleted.</p>
    <label class="check"><input type="checkbox" id="delFiles"><span>Also delete the project folder (your ROM copy and exported sources in it)</span></label>
    <div class="row"><button class="primary" id="delGo" type="button">Remove</button><button class="ghost" data-close type="button">Cancel</button></div>`);
  $("delGo").onclick = async () => {
    try {
      await api("/api/delete", { method: "POST", body: { slug: S.slug, files: $("delFiles").checked } });
      $("dialog").hidden = true;
      location.hash = "#/";
    } catch (e) { toast(esc(e.message), "err"); }
  };
};
$("quitBtn").onclick = async () => {
  if (!confirm("Quit decomp.wtf? Running agents stop.")) return;
  await api("/api/quit", { method: "POST" }).catch(() => {});
  document.body.innerHTML = `<main class="view"><p class="muted">decomp.wtf has stopped. You can close this tab.</p></main>`;
};

route();
