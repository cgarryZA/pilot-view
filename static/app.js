import { createScene } from '/static/scene.js';
import { createCalibration } from '/static/calibration.js';

const $ = (id) => document.getElementById(id);

const els = {
  clock: $('clock'),
  date: $('date'),
  sourcePill: $('source-pill'),
  serverPill: $('server-pill'),
  cameraPill: $('camera-pill'),

  disconnected: $('disconnected-view'),
  connected: $('connected-view'),
  sceneBackground: $('scene-background'),
  sceneCanvas: $('scene-canvas'),
  viewToggle: $('view-toggle'),
  calibPanel: $('calib-panel'),
  pageTitle: $('page-title'),
  pageSubtitle: $('page-subtitle'),
  nav: $('nav'),

  camModel: $('cam-model'),
  camIface: $('cam-iface'),
  camSerial: $('cam-serial'),
  camFirmware: $('cam-firmware'),
  camLast: $('cam-last'),
  camError: $('cam-error'),

  hudState: $('hud-state'),
  hudStateValue: () => document.querySelector('#hud-state .hud-state-value'),
  clearances: {
    front: $('cl-front'),
    rear: $('cl-rear'),
    left: $('cl-left'),
    right: $('cl-right'),
    ceiling: $('cl-ceiling'),
  },

  streamStatus: $('stream-status'),
  latency: $('latency'),
  buildId: $('build-id'),
};

const pad = (n) => String(n).padStart(2, '0');

function tickClock() {
  const d = new Date();
  els.clock.textContent = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  els.date.textContent = d
    .toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' })
    .toUpperCase();
}
tickClock();
setInterval(tickClock, 30_000);

function fmtTime(iso) {
  if (!iso) return 'never';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleTimeString();
}

function fmtMetres(v) {
  if (v == null || isNaN(v)) return '—';
  if (v < 0) return `${(v * 100).toFixed(0)} cm!`;
  if (v < 1) return `${(v * 100).toFixed(0)} cm`;
  return `${v.toFixed(2)} m`;
}

function setPill(pill, online, label) {
  const dot = pill.querySelector('.dot');
  const text = pill.querySelector('.pill-text');
  if (dot) {
    dot.classList.toggle('dot-online', online);
    dot.classList.toggle('dot-offline', !online);
  }
  pill.classList.toggle('online', online);
  if (label !== undefined) text.textContent = label;
}

function classifyClearance(value, thresholds) {
  if (value < thresholds.danger) return 'danger';
  if (value < thresholds.warn) return 'warning';
  return 'safe';
}

// ─── Scene lifecycle ─────────────────────────────────
let scene = null;
let calib = null;
let viewMode = 'live';
let appMode = 'overview';
let currentLiveUrl = null;

function ensureScene() {
  if (scene) return scene;
  scene = createScene(els.sceneCanvas);
  scene.setMode(viewMode);
  calib = createCalibration({ scene, panel: els.calibPanel });
  return scene;
}

function showDisconnected() {
  els.disconnected.classList.remove('hidden');
  els.connected.classList.add('hidden');
}

function showConnected() {
  els.disconnected.classList.add('hidden');
  els.connected.classList.remove('hidden');
  ensureScene();
}

function setViewMode(next) {
  if (next === viewMode) return;
  viewMode = next;
  for (const btn of els.viewToggle.querySelectorAll('.view-toggle-btn')) {
    const active = btn.dataset.mode === next;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-selected', active ? 'true' : 'false');
  }
  els.connected.dataset.mode = next;
  // Hide background when in orbit mode
  els.sceneBackground.classList.toggle('visible', next === 'live' && !!currentLiveUrl);
  if (scene) scene.setMode(next);
}

els.viewToggle.addEventListener('click', (e) => {
  const btn = e.target.closest('.view-toggle-btn');
  if (!btn) return;
  setViewMode(btn.dataset.mode);
});

// ─── App-level mode (overview vs calibration) ────────
const PAGE_META = {
  overview: { title: 'Overview', subtitle: 'Garage vision & parking assist' },
  calibration: { title: 'Calibration', subtitle: 'Align scene to real garage & vehicle' },
};

function setAppMode(next) {
  if (!PAGE_META[next]) return;
  if (next === appMode) return;
  appMode = next;
  els.connected.dataset.appMode = next;
  els.pageTitle.textContent = PAGE_META[next].title;
  els.pageSubtitle.textContent = PAGE_META[next].subtitle;
  for (const item of els.nav.querySelectorAll('.nav-item')) {
    item.classList.toggle('active', item.dataset.view === next);
  }
}

els.nav.addEventListener('click', (e) => {
  const item = e.target.closest('.nav-item');
  if (!item || item.classList.contains('disabled')) return;
  const view = item.dataset.view;
  setAppMode(view);
});

document.addEventListener('app-mode', (e) => setAppMode(e.detail));

function setLiveBackground(url) {
  if (url === currentLiveUrl) return;
  currentLiveUrl = url;
  if (url) {
    els.sceneBackground.style.backgroundImage = `url("${url}")`;
    if (viewMode === 'live') els.sceneBackground.classList.add('visible');
  } else {
    els.sceneBackground.style.backgroundImage = '';
    els.sceneBackground.classList.remove('visible');
  }
}

// ─── State application ──────────────────────────────
function applyState(payload) {
  const c = payload.camera || {};
  const g = payload.geometry;

  // Source pill
  setPill(els.sourcePill, true, (payload.source || 'unknown').toUpperCase());

  // Camera pill
  setPill(els.cameraPill, !!c.connected, 'Camera');

  // Disconnected card details (kept fresh even when connected, in case we toggle back)
  els.camModel.textContent = c.model || '—';
  els.camIface.textContent = c.interface || '—';
  els.camSerial.textContent = c.serial || '—';
  els.camFirmware.textContent = c.firmware || '—';
  els.camLast.textContent = fmtTime(c.last_attempt);
  els.camError.textContent = c.error || 'none';

  if (c.connected && g) {
    showConnected();
    setLiveBackground(payload.live_url);
    scene.applyGeometry(g);

    // HUD state pill
    const state = g.state || 'safe';
    els.hudState.classList.remove('state-safe', 'state-warning', 'state-danger');
    els.hudState.classList.add(`state-${state}`);
    els.hudStateValue().textContent = state.toUpperCase();

    // Clearance cells
    const thresholds = g.thresholds || { warn: 0.5, danger: 0.2 };
    for (const [key, el] of Object.entries(els.clearances)) {
      const v = g.clearances?.[key];
      el.textContent = fmtMetres(v);
      const cell = el.closest('.hud-cell');
      cell.classList.remove('warn', 'danger');
      if (v != null) {
        const cls = classifyClearance(v, thresholds);
        if (cls === 'warning') cell.classList.add('warn');
        if (cls === 'danger') cell.classList.add('danger');
      }
    }

    els.streamStatus.textContent = 'live';
  } else {
    showDisconnected();
    els.streamStatus.textContent = 'idle';
  }
}

// ─── WebSocket ──────────────────────────────────────
let ws;
let lastPing = 0;

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => {
    setPill(els.serverPill, true, 'Server');
    lastPing = performance.now();
  };

  ws.onmessage = (ev) => {
    const now = performance.now();
    els.latency.textContent = `${Math.round(now - lastPing)} ms`;
    lastPing = now;
    try {
      applyState(JSON.parse(ev.data));
    } catch (err) {
      console.error('bad payload', err);
    }
  };

  ws.onclose = () => {
    setPill(els.serverPill, false, 'Server');
    els.streamStatus.textContent = 'reconnecting';
    setTimeout(connect, 1500);
  };

  ws.onerror = () => ws.close();
}

connect();
