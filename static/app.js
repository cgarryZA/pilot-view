import { createScene } from '/static/scene.js';

const $ = (id) => document.getElementById(id);

const els = {
  clock: $('clock'),
  date: $('date'),
  sourcePill: $('source-pill'),
  serverPill: $('server-pill'),
  cameraPill: $('camera-pill'),

  disconnected: $('disconnected-view'),
  connected: $('connected-view'),
  sceneCanvas: $('scene-canvas'),

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
function ensureScene() {
  if (scene) return scene;
  scene = createScene(els.sceneCanvas);
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
