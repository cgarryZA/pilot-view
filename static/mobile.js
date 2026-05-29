import { createScene } from '/static/scene.js';

const $ = (id) => document.getElementById(id);

const els = {
  bg: $('m-bg'),
  canvas: $('m-canvas'),
  chipSource: $('chip-source'),
  chipConn: $('chip-conn'),
  connText: $('conn-text'),
  viewToggle: $('viewtoggle'),
  stateBadge: $('state-badge'),
  stateText: $('state-text'),
  disc: $('disc'),
  discSub: $('disc-sub'),
  clearances: $('clearances'),
  ctlDoor: $('ctl-door'),
  doorSub: $('door-sub'),
  ctlLights: $('ctl-lights'),
  lightsSub: $('lights-sub'),
  vehName: $('veh-name'),
  batText: $('bat-text'),
  envText: $('env-text'),
  toast: $('toast'),
  cl: {
    front: $('cl-front'), rear: $('cl-rear'), left: $('cl-left'),
    right: $('cl-right'), ceiling: $('cl-ceiling'),
  },
};

const DOOR_LABELS = {
  closed: 'Closed', open: 'Open', partial: 'Moving…', fault: 'Fault', unknown: '—',
};

let scene = null;
let viewMode = 'live';
let currentLiveUrl = null;
let currentVehicleId = null;

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
function showToast(msg, kind = 'info', ms = 2200) {
  els.toast.textContent = msg;
  els.toast.className = `m-toast show ${kind}`;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { els.toast.className = 'm-toast'; }, ms);
}

// ── view toggle ──
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
  const show = viewMode === 'live' && currentLiveUrl && !els.disc.classList.contains('hidden') === false;
  // visible only in live mode, when we have a url AND we're connected (disc hidden)
  const connected = els.disc.classList.contains('hidden');
  els.bg.classList.toggle('visible', viewMode === 'live' && !!currentLiveUrl && connected);
}

function setLiveBackground(url) {
  if (url !== currentLiveUrl) {
    currentLiveUrl = url || null;
    if (url) els.bg.src = url; else els.bg.removeAttribute('src');
  }
  updateBgVisibility();
}

// ── controls ──
els.ctlDoor.addEventListener('click', async () => {
  try {
    const res = await fetch('/api/door/toggle', { method: 'POST' });
    if (res.ok) {
      const data = await res.json().catch(() => ({}));
      if (data?.blocked_reason) showToast(data.blocked_reason, 'warn');
    } else {
      showToast(`Door: HTTP ${res.status}`, 'danger');
    }
  } catch (err) { showToast('Door toggle failed', 'danger'); }
});
els.ctlLights.addEventListener('click', async () => {
  try { await fetch('/api/lights/toggle', { method: 'POST' }); }
  catch (err) { showToast('Lights toggle failed', 'danger'); }
});

// ── state application ──
function applyState(p) {
  const c = p.camera || {};
  const g = p.geometry;

  els.chipSource.textContent = (p.source || '—').toUpperCase();

  // door
  const doorStatus = (p.door && p.door.status) || 'unknown';
  els.doorSub.textContent = DOOR_LABELS[doorStatus] || '—';
  els.ctlDoor.className = `m-ctl ${doorStatus}`;
  els.ctlDoor.disabled = doorStatus === 'fault';
  if (scene && scene.applyDoor) scene.applyDoor(p.door);

  // lights
  const lights = p.lights;
  if (lights == null) { els.lightsSub.textContent = '?'; els.ctlLights.classList.remove('on'); }
  else { els.lightsSub.textContent = lights.on ? 'On' : 'Off'; els.ctlLights.classList.toggle('on', !!lights.on); }

  // vehicle
  const v = p.vehicles;
  els.vehName.textContent = v?.active?.name || (v?.active_id ? v.active_id : 'unknown');
  if (v?.active && scene) {
    scene.applyActiveVehicle(v.active);
    currentVehicleId = v.active_id;
  }

  // battery
  const b = p.battery;
  if (b && b.available) {
    const bits = [];
    if (b.voltage != null) bits.push(`${Number(b.voltage).toFixed(1)} V`);
    if (b.percent != null) bits.push(`${Math.round(b.percent)}%`);
    els.batText.textContent = (bits.join(' · ') || '—') + (b.charging ? ' ⚡' : '');
  } else {
    els.batText.textContent = '—';
  }

  // environment
  const env = p.environment || {};
  const t = env.temperature_c, h = env.humidity_pct;
  els.envText.textContent = (t != null ? `${t}°C` : '—') + (h != null ? ` · ${h}%` : '');

  // connection / scene
  const connected = !!c.connected && !!g;
  els.chipConn.classList.toggle('ok', connected);
  els.connText.textContent = connected ? 'live' : 'no signal';

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
    for (const [key, el] of Object.entries(els.cl)) {
      const val = g.clearances?.[key];
      el.textContent = fmtM(val);
      const cell = el.closest('.m-clear-cell');
      cell.classList.remove('warn', 'danger');
      const cls = classifyClearance(val, thr);
      if (cls === 'warning') cell.classList.add('warn');
      if (cls === 'danger') cell.classList.add('danger');
    }
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
    els.chipConn.classList.remove('ok');
    els.connText.textContent = 'reconnecting';
    setTimeout(connect, 1500);
  };
  ws.onerror = () => { try { ws.close(); } catch (e) {} };
}

async function start() {
  scene = createScene(els.canvas);
  scene.setMode('live');
  // Apply calibration (camera pose, garage dims) once on load.
  try {
    const cal = await fetch('/api/calibration', { credentials: 'same-origin' }).then((r) => r.ok ? r.json() : null);
    if (cal && scene.applyCalibration) scene.applyCalibration(cal);
  } catch (e) { /* non-fatal */ }
  connect();
}

start();
