import { createScene } from '/static/scene.js';

const $ = (id) => document.getElementById(id);

const els = {
  bg: $('m-bg'),
  canvas: $('m-canvas'),
  chipSource: $('chip-source'),
  chipVehicle: $('chip-vehicle'),
  tabAlign: $('tab-align'),
  srcName: $('src-name'),
  connDot: $('conn-dot'),
  viewToggle: $('viewtoggle'),
  stateBadge: $('state-badge'),
  stateText: $('state-text'),
  disc: $('disc'),
  discSub: $('disc-sub'),
  clearances: $('clearances'),
  toast: $('toast'),
  cl: {
    front: $('cl-front'), rear: $('cl-rear'), left: $('cl-left'),
    right: $('cl-right'), ceiling: $('cl-ceiling'),
  },
};

let scene = null;
let viewMode = 'live';
let currentLiveUrl = null;
let connected = false;
let currentSource = null;
let vehicleDriver = null;

function fmtM(v) {
  if (v == null || isNaN(v)) return '—';
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

// ── source toggle: synthetic <-> camera ──
els.chipSource.addEventListener('click', async () => {
  const next = currentSource === 'orbbec' ? 'disconnected' : 'orbbec';
  try {
    const res = await fetch('/api/source', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: next }),
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok) showToast(`Source: ${data.current || next}`);
    else showToast(data.detail || `Source switch failed`, 'danger');
  } catch (err) { showToast('Source switch failed', 'danger'); }
});

// ── vehicle cycle (synthetic driver only) ──
els.chipVehicle.addEventListener('click', async () => {
  if (vehicleDriver !== 'synthetic') return;
  try { await fetch('/api/vehicles/cycle', { method: 'POST' }); }
  catch (err) { /* ignore */ }
});

// ── auto-align the camera pose from depth (calibrate in the garage) ──
els.tabAlign.addEventListener('click', async () => {
  showToast('Reading depth…');
  try {
    const res = await fetch('/api/calibration/auto_pose', { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { showToast(data.detail || 'Align failed', 'danger'); return; }
    showToast(`Aligned · floor ${data.floor_height} m · wall ${data.wall_distance} m`, 'info', 2600);
    // Pull the saved pose into the scene immediately.
    const cal = await fetch('/api/calibration', { credentials: 'same-origin' }).then((r) => r.ok ? r.json() : null);
    if (cal && scene && scene.applyCalibration) scene.applyCalibration(cal);
  } catch (err) {
    showToast('Align error', 'danger');
  }
});

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
  els.chipVehicle.textContent = v?.active?.name || (v?.active_id || 'no vehicle');
  els.chipVehicle.classList.toggle('tap', vehicleDriver === 'synthetic');
  if (v?.active && scene) scene.applyActiveVehicle(v.active);

  connected = !!c.connected && !!g;
  els.connDot.classList.toggle('ok', connected);

  if (connected) {
    els.disc.classList.add('hidden');
    els.stateBadge.classList.remove('hidden');
    els.clearances.classList.remove('hidden');
    setLiveBackground(p.live_url);
    scene.applyGeometry(g);

    const state = g.state || 'safe';
    els.stateBadge.className = `m-state state-${state}`;
    els.stateText.textContent = state.toUpperCase();

    const thr = g.thresholds || { warn: 0.5, danger: 0.2 };
    const hasClear = g.clearances && Object.keys(g.clearances).length > 0;
    for (const [key, el] of Object.entries(els.cl)) {
      const val = g.clearances?.[key];
      el.textContent = fmtM(val);
      const cell = el.closest('.m-clear-cell');
      cell.classList.remove('warn', 'danger');
      const cls = classifyClearance(val, thr);
      if (cls === 'warning') cell.classList.add('warn');
      if (cls === 'danger') cell.classList.add('danger');
    }
    // No car detected yet (e.g. camera live but detection not running): dim the strip.
    els.clearances.style.opacity = hasClear ? '1' : '0.45';
  } else {
    els.disc.classList.remove('hidden');
    els.stateBadge.classList.add('hidden');
    els.clearances.classList.add('hidden');
    els.discSub.textContent = c.error || 'Waiting for the camera…';
    updateBgVisibility();
  }
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
