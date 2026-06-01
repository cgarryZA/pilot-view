import { createScene } from '/static/scene.js';

const $ = (id) => document.getElementById(id);

const els = {
  bg: $('m-bg'),
  canvas: $('m-canvas'),
  chipSource: $('chip-source'),
  chipVehicle: $('chip-vehicle'),
  chipMute: $('chip-mute'),
  tabAlign: $('tab-align'),
  calSheet: $('cal-sheet'),
  calClose: $('cal-close'),
  calAlign: $('cal-align'),
  calStatus: $('cal-status'),
  srcName: $('src-name'),
  connDot: $('conn-dot'),
  viewToggle: $('viewtoggle'),
  stateBadge: $('state-badge'),
  stateText: $('state-text'),
  stopBanner: $('stop-banner'),
  stopText: $('stop-text'),
  disc: $('disc'),
  discSub: $('disc-sub'),
  clearances: $('clearances'),
  toast: $('toast'),
  cl: {
    front: $('cl-front'), rear: $('cl-rear'), left: $('cl-left'),
    right: $('cl-right'), ceiling: $('cl-ceiling'),
  },
};

const SIDE_LABEL = { front: 'FRONT', rear: 'REAR', left: 'LEFT', right: 'RIGHT', ceiling: 'ROOF' };

let scene = null;
let viewMode = 'live';
let currentLiveUrl = null;
let connected = false;
let currentSource = null;
let vehicleDriver = null;
let vehicleList = [];
let activeVehicleId = null;

// ── audible / haptic proximity cue ──────────────────────────────────────
// A reversing driver is looking over their shoulder, not at the phone. The beep
// period shrinks as the nearest side closes in; a solid tone at danger. Web
// Audio must be unlocked by a user gesture (mobile autoplay policy).
let muted = false;
try { muted = localStorage.getItem('pv_muted') === '1'; } catch (e) {}
let audioCtx = null;
let beepTimer = null;
let cueState = null;
let cuePeriod = 0;

function unlockAudio() {
  if (audioCtx) { if (audioCtx.state === 'suspended') audioCtx.resume(); return; }
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (AC) audioCtx = new AC();
  } catch (e) { audioCtx = null; }
}

function beep(freq, ms) {
  if (!audioCtx || muted) return;
  try {
    const o = audioCtx.createOscillator();
    const g = audioCtx.createGain();
    o.type = 'square'; o.frequency.value = freq;
    g.gain.value = 0.0001;
    o.connect(g); g.connect(audioCtx.destination);
    const t = audioCtx.currentTime;
    g.gain.exponentialRampToValueAtTime(0.18, t + 0.01);
    g.gain.exponentialRampToValueAtTime(0.0001, t + ms / 1000);
    o.start(t); o.stop(t + ms / 1000 + 0.02);
  } catch (e) {}
}

function clearBeep() { if (beepTimer) { clearInterval(beepTimer); beepTimer = null; } }

// Drive the repeating cue from the current state + nearest clearance. Called
// every frame (~15 Hz) so it MUST be idempotent: only re-arm when the cue
// actually changes, or the interval would reset before it ever fires.
function updateCue(state, minClear) {
  let period = 0;
  if (state === 'danger') period = 180;
  else if (state === 'warning') {
    // ~600 ms far → ~220 ms close to danger; quantised so tiny jitter doesn't re-arm.
    const raw = Math.max(220, Math.min(600, 220 + (minClear ?? 0.5) * 800));
    period = Math.round(raw / 60) * 60;
  }

  // One-shot pleasant confirm on entering 'parked'.
  if (state === 'parked' && cueState !== 'parked' && !muted) {
    beep(523, 120); setTimeout(() => beep(784, 170), 150);
    if (navigator.vibrate) navigator.vibrate([60, 50, 60]);
  }

  if (state === cueState && period === cuePeriod) return;
  cueState = state; cuePeriod = period;
  clearBeep();
  if (muted) return;

  if (state === 'danger') {
    beep(880, 130);
    beepTimer = setInterval(() => { beep(880, 130); if (navigator.vibrate) navigator.vibrate(120); }, period);
  } else if (state === 'warning') {
    beep(660, 90);
    beepTimer = setInterval(() => { beep(660, 90); if (navigator.vibrate) navigator.vibrate(30); }, period);
  }
}

// Driver-facing labels for the state badge (never the raw backend token).
const STATE_TEXT = {
  safe: 'CLEAR', parked: 'PARKED · OK', warning: 'WARNING',
  danger: 'DANGER', searching: 'SEARCHING…', uncalibrated: 'RUN ALIGN',
};

function setMuted(v) {
  muted = v;
  try { localStorage.setItem('pv_muted', v ? '1' : '0'); } catch (e) {}
  els.chipMute.textContent = v ? '🔇' : '🔊';
  els.chipMute.setAttribute('aria-pressed', v ? 'true' : 'false');
  if (v && beepTimer) { clearInterval(beepTimer); beepTimer = null; }
}
els.chipMute.addEventListener('click', () => { unlockAudio(); setMuted(!muted); });
setMuted(muted);

// ── screen wake lock — keep the HUD lit through the whole maneuver ──
let wakeLock = null;
async function acquireWakeLock() {
  try { if (navigator.wakeLock && !wakeLock) wakeLock = await navigator.wakeLock.request('screen'); }
  catch (e) { /* user gesture / unsupported — non-fatal */ }
}
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') acquireWakeLock();
});

function fmtM(v) {
  if (v == null || isNaN(v)) return '—';
  if (v <= 0) return 'TOUCH';                  // overlap / contact — unmistakable
  if (v < 1) return `${Math.round(v * 100)} cm`;
  return `${v.toFixed(2)} m`;
}

function classifyClearance(v, t) {
  if (v == null) return 'safe';
  if (v < (t?.danger ?? 0.2)) return 'danger';
  if (v < (t?.warn ?? 0.5)) return 'warning';
  return 'safe';
}

let toastTimer = null;
function showToast(msg, kind = 'info', ms = 2000) {
  els.toast.textContent = msg;
  els.toast.className = `m-toast show ${kind}`;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { els.toast.className = 'm-toast'; }, ms);
}

// ── view toggle (live photo vs orbitable 3D) ──
function setViewMode(next) {
  if (next === viewMode) return;
  viewMode = next;
  for (const b of els.viewToggle.querySelectorAll('button')) {
    b.classList.toggle('active', b.dataset.mode === next);
  }
  if (scene) scene.setMode(next);
  updateBgVisibility();
}
els.viewToggle.addEventListener('click', (e) => {
  const b = e.target.closest('button');
  if (b) setViewMode(b.dataset.mode);
});

function updateBgVisibility() {
  els.bg.classList.toggle('visible', viewMode === 'live' && !!currentLiveUrl && connected);
}

function setLiveBackground(url) {
  if (url !== currentLiveUrl) {
    currentLiveUrl = url || null;
    if (url) els.bg.src = url; else els.bg.removeAttribute('src');
  }
  updateBgVisibility();
}

// ── source toggle: camera (orbbec) <-> disconnected ──
els.chipSource.addEventListener('click', async () => {
  unlockAudio();
  const next = currentSource === 'orbbec' ? 'disconnected' : 'orbbec';
  try {
    const res = await fetch('/api/source', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: next }),
      credentials: 'same-origin',
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok) showToast(`Source: ${data.current || next}`);
    else showToast(data.detail || `Source switch failed`, 'danger');
  } catch (err) { showToast('Source switch failed', 'danger'); }
});

// ── vehicle picker: cycle the active vehicle. Works in any driver mode so the
// driver always has a manual override if the beacon mis-IDs or isn't present. ──
els.chipVehicle.addEventListener('click', async () => {
  if (vehicleList.length < 2 && !activeVehicleId) return;
  try {
    const res = await fetch('/api/vehicles/cycle', { method: 'POST', credentials: 'same-origin' });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data) showToast(`Vehicle: ${(data.active && data.active.name) || data.active_id || '—'}`);
  } catch (err) { showToast('Could not change vehicle', 'danger'); }
});

// ── calibration sheet: auto-align + measure, with editable garage dims ──
const calInputs = els.calSheet ? Array.from(els.calSheet.querySelectorAll('input[data-path]')) : [];

function getPath(obj, path) {
  return path.split('.').reduce((o, k) => (o == null ? o : o[k]), obj);
}

async function loadCalIntoSheet(applyToScene = true) {
  const cal = await fetch('/api/calibration', { credentials: 'same-origin' }).then((r) => r.ok ? r.json() : null);
  if (!cal) return;
  for (const inp of calInputs) {
    const v = getPath(cal, inp.dataset.path);
    if (v != null) inp.value = v;
  }
  if (applyToScene && scene && scene.applyCalibration) scene.applyCalibration(cal);
}

els.tabAlign.addEventListener('click', () => {
  els.calSheet.classList.remove('hidden');
  loadCalIntoSheet();
});
els.calClose.addEventListener('click', () => els.calSheet.classList.add('hidden'));

els.calAlign.addEventListener('click', async () => {
  unlockAudio();
  els.calStatus.textContent = 'Reading depth…';
  try {
    const res = await fetch('/api/calibration/auto_pose', { method: 'POST', credentials: 'same-origin' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const d = (data.detail || '').toLowerCase();
      els.calStatus.textContent = d.includes('no depth')
        ? 'No depth yet — is the camera connected and streaming?'
        : (d.includes('floor')
            ? 'Couldn’t find the floor + a facing wall. Clear the bay and make sure the camera sees the floor and the far wall, then retry.'
            : (data.detail || 'Align failed.'));
      return;
    }
    const g = data.garage || {};
    const cl = data.garage_clamped || {};
    const flag = (k) => (cl[k] ? '⚠︎' : '');
    const conf = (data.floor_points != null && data.wall_points != null)
      ? ` · floor ${data.floor_points} / wall ${data.wall_points} pts` : '';
    const anyClamp = cl.width || cl.length || cl.height;
    els.calStatus.textContent =
      `Aligned · ${g.width}${flag('width')} × ${g.length}${flag('length')} × ${g.height}${flag('height')} m${conf}.` +
      (anyClamp ? '  ⚠︎ hit a limit (a wall may not be fully in view) — check & nudge it.' : '  Nudge any value if needed.');
    await loadCalIntoSheet();   // refresh fields with the measured dims + apply pose
  } catch (err) {
    els.calStatus.textContent = 'Align error — try again.';
  }
});

// Nudge a dimension → save (deep-merged) and re-apply to the scene live.
for (const inp of calInputs) {
  inp.addEventListener('change', async () => {
    const val = parseFloat(inp.value);
    if (isNaN(val)) return;
    const keys = inp.dataset.path.split('.');
    const body = {};
    let node = body;
    keys.slice(0, -1).forEach((k) => { node[k] = {}; node = node[k]; });
    node[keys[keys.length - 1]] = val;
    try {
      await fetch('/api/calibration', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body), credentials: 'same-origin',
      });
      await loadCalIntoSheet();   // re-apply to scene with the nudged value
    } catch (err) { /* ignore */ }
  });
}

// ── state application ──
function applyState(p) {
  const c = p.camera || {};
  const g = p.geometry;

  currentSource = p.source || null;
  els.srcName.textContent = (p.source || '—').toUpperCase();

  // roller-door panel in the 3D scene still animates from door status if present
  if (scene && scene.applyDoor) scene.applyDoor(p.door);

  // vehicle
  const v = p.vehicles;
  vehicleDriver = v?.driver || null;
  vehicleList = v?.list || [];
  activeVehicleId = v?.active_id || null;
  els.chipVehicle.textContent = v?.active?.name || (v?.active_id || 'no vehicle');
  els.chipVehicle.classList.toggle('tap', vehicleList.length > 1 || !!activeVehicleId);
  if (v?.active && scene) scene.applyActiveVehicle(v.active);

  connected = !!c.connected && !!g;
  els.connDot.classList.toggle('ok', connected);

  if (!connected) {
    els.disc.classList.remove('hidden');
    els.stateBadge.classList.add('hidden');
    els.clearances.classList.add('hidden');
    els.stopBanner.classList.add('hidden');
    els.discSub.textContent = c.error || 'Waiting for the camera…';
    updateBgVisibility();
    updateCue('none');
    return;
  }

  acquireWakeLock();
  els.disc.classList.add('hidden');
  els.stateBadge.classList.remove('hidden');
  setLiveBackground(p.live_url);
  scene.applyGeometry(g);

  const state = g.state || 'safe';
  els.stateBadge.className = `m-state state-${state}`;
  els.stateText.textContent = STATE_TEXT[state] || state.toUpperCase();

  const thr = g.thresholds || { warn: 0.5, danger: 0.2 };
  const clear = g.clearances || {};
  const hasClear = Object.keys(clear).length > 0;

  // Controlling side = the smallest clearance (what the driver must watch).
  let ctrlKey = null, ctrlVal = Infinity;
  for (const [k, val] of Object.entries(clear)) {
    if (val != null && val < ctrlVal) { ctrlVal = val; ctrlKey = k; }
  }

  if (hasClear) {
    els.clearances.classList.remove('hidden');
    els.clearances.style.opacity = '1';
    for (const [key, el] of Object.entries(els.cl)) {
      const val = clear[key];
      el.textContent = fmtM(val);
      const cell = el.closest('.m-clear-cell');
      cell.classList.remove('warn', 'danger', 'controlling');
      const cls = classifyClearance(val, thr);
      if (cls === 'warning') cell.classList.add('warn');
      if (cls === 'danger') cell.classList.add('danger');
      if (key === ctrlKey) cell.classList.add('controlling');
    }
  } else {
    // No car-sized cluster (searching / uncalibrated): hide the strip rather than
    // show misleading numbers or a dim-green "all clear".
    els.clearances.classList.add('hidden');
  }

  // STOP / proximity banner — the one thing readable mid-reverse.
  if (hasClear && (state === 'danger' || state === 'warning')) {
    const side = SIDE_LABEL[ctrlKey] || '';
    const dist = fmtM(ctrlVal);
    els.stopBanner.classList.remove('hidden');
    els.stopBanner.classList.toggle('danger', state === 'danger');
    els.stopBanner.classList.toggle('warn', state === 'warning');
    els.stopText.textContent = state === 'danger' ? `STOP · ${side} ${dist}` : `${side} ${dist}`;
  } else {
    els.stopBanner.classList.add('hidden');
  }

  updateCue(state, ctrlVal);
}

// ── websocket ──
let ws = null;
function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = (ev) => {
    try { applyState(JSON.parse(ev.data)); }
    catch (err) { console.error('bad payload', err); }
  };
  ws.onclose = () => {
    connected = false;
    els.connDot.classList.remove('ok');
    setTimeout(connect, 1500);
  };
  ws.onerror = () => { try { ws.close(); } catch (e) {} };
}

async function start() {
  scene = createScene(els.canvas);
  scene.setMode('live');
  try {
    const cal = await fetch('/api/calibration', { credentials: 'same-origin' }).then((r) => r.ok ? r.json() : null);
    if (cal && scene.applyCalibration) scene.applyCalibration(cal);
  } catch (e) { /* non-fatal */ }
  connect();
}

start();
