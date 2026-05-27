import { createScene } from '/static/scene.js';
import { createCalibration } from '/static/calibration.js';
import * as auth from '/static/auth.js';

const $ = (id) => document.getElementById(id);

const els = {
  clock: $('clock'),
  date: $('date'),
  sourcePill: $('source-pill'),
  serverPill: $('server-pill'),
  cameraPill: $('camera-pill'),
  doorPill: $('door-pill'),
  lightsPill: $('lights-pill'),
  vehiclePill: $('vehicle-pill'),
  batteryPill: $('battery-pill'),
  tempPill: $('temp-pill'),
  humidityPill: $('humidity-pill'),
  authOverlay: $('auth-overlay'),
  authHeadline: $('auth-headline'),
  authSub: $('auth-sub'),
  authNicknameRow: $('auth-nickname-row'),
  authNicknameInput: $('auth-nickname'),
  authPrimaryBtn: $('auth-primary-btn'),
  authError: $('auth-error'),
  authFoot: $('auth-foot'),

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

const DOOR_LABELS = {
  closed: 'Door · closed',
  open: 'Door · open',
  partial: 'Door · moving',
  fault: 'Door · sensor fault',
  unknown: 'Door · unknown',
};

const DOOR_STATE_CLASSES = ['door-closed', 'door-open', 'door-partial', 'door-fault', 'door-unknown'];
const DOT_CLASSES = ['dot-online', 'dot-offline', 'dot-warn', 'dot-danger'];

function clearClasses(el, classes) {
  for (const c of classes) el.classList.remove(c);
}

function applyDoorState(d) {
  const status = (d && d.status) || 'unknown';
  const pill = els.doorPill;
  const dot = pill.querySelector('.dot');
  const text = pill.querySelector('.pill-text');

  clearClasses(pill, DOOR_STATE_CLASSES);
  pill.classList.add(`door-${status}`);

  clearClasses(dot, DOT_CLASSES);
  dot.classList.remove('pulse');

  if (status === 'closed') dot.classList.add('dot-online');
  else if (status === 'open') dot.classList.add('dot-warn');
  else if (status === 'partial') { dot.classList.add('dot-online'); dot.classList.add('pulse'); }
  else if (status === 'fault') dot.classList.add('dot-danger');
  else dot.classList.add('dot-offline');

  text.textContent = DOOR_LABELS[status] || DOOR_LABELS.unknown;

  // Refuse clicks during fault state.
  const blocked = status === 'fault';
  pill.disabled = blocked;
  pill.style.opacity = blocked ? '0.75' : '';
}

const LIGHTS_STATE_CLASSES = ['lights-on', 'lights-off', 'lights-unknown'];

function applyLightsState(l) {
  const pill = els.lightsPill;
  const dot = pill.querySelector('.dot');
  const text = pill.querySelector('.pill-text');

  clearClasses(pill, LIGHTS_STATE_CLASSES);
  clearClasses(dot, DOT_CLASSES);

  if (l == null) {
    pill.classList.add('lights-unknown');
    dot.classList.add('dot-offline');
    text.textContent = 'Lights · ?';
    return;
  }

  if (l.on) {
    pill.classList.add('lights-on');
    dot.classList.add('dot-warn');
    text.textContent = 'Lights · on';
  } else {
    pill.classList.add('lights-off');
    dot.classList.add('dot-offline');
    text.textContent = 'Lights · off';
  }
}

async function toggleDoor() {
  try {
    await fetch('/api/door/toggle', { method: 'POST' });
  } catch (err) {
    console.error('[door] toggle failed', err);
  }
}

async function toggleLights() {
  try {
    await fetch('/api/lights/toggle', { method: 'POST' });
  } catch (err) {
    console.error('[lights] toggle failed', err);
  }
}

els.doorPill.addEventListener('click', toggleDoor);
els.lightsPill.addEventListener('click', toggleLights);

let currentActiveVehicle = null;

// Drop any stale quality-cycler preferences from when the temporary pill existed.
try { localStorage.removeItem('pilot-view.vehicle-quality'); } catch {}

const ENV_STATE_CLASSES = ['env-safe', 'env-warn', 'env-unavailable'];

function applyEnvPill(pill, value, unit, thresholds) {
  const dot = pill.querySelector('.dot');
  const text = pill.querySelector('.pill-text');
  clearClasses(pill, ENV_STATE_CLASSES);
  clearClasses(dot, DOT_CLASSES);

  if (value == null || isNaN(value)) {
    pill.classList.add('env-unavailable');
    dot.classList.add('dot-offline');
    text.textContent = `— ${unit}`;
    return;
  }

  const out = thresholds && (value < thresholds.warn_low || value > thresholds.warn_high);
  pill.classList.add(out ? 'env-warn' : 'env-safe');
  dot.classList.add(out ? 'dot-warn' : 'dot-online');
  text.textContent = `${value}${unit}`;
}

let currentCalibration = null;
let activeVehicleId = null;
let vehicleDriverName = null;

function applyEnvironment(env) {
  const t = currentCalibration?.environment?.temperature;
  const h = currentCalibration?.environment?.humidity;
  applyEnvPill(els.tempPill, env?.temperature_c, ' °C', t);
  applyEnvPill(els.humidityPill, env?.humidity_pct, '%', h);
}

const VEHICLE_STATE_CLASSES = ['vehicle-known', 'vehicle-unknown'];

function applyVehicleState(v) {
  const pill = els.vehiclePill;
  const dot = pill.querySelector('.dot');
  const text = pill.querySelector('.pill-text');
  clearClasses(pill, VEHICLE_STATE_CLASSES);
  clearClasses(dot, DOT_CLASSES);

  vehicleDriverName = v?.driver || null;
  const known = !!v?.active_id;

  if (known) {
    pill.classList.add('vehicle-known');
    dot.classList.add('dot-online');
    const name = v.active?.name || v.active_id;
    text.textContent = name;
  } else {
    pill.classList.add('vehicle-unknown');
    dot.classList.add('dot-offline');
    text.textContent = 'Vehicle · unknown';
  }

  // Only synthetic mode lets you click to cycle.
  pill.disabled = vehicleDriverName !== 'synthetic';
  pill.style.cursor = vehicleDriverName === 'synthetic' ? 'pointer' : 'default';

  // Notify calibration if active vehicle changed
  if (v?.active_id !== activeVehicleId) {
    activeVehicleId = v?.active_id || null;
    if (calib && calib.setActiveVehicle) calib.setActiveVehicle(activeVehicleId);
  }
}

async function cycleVehicle() {
  if (vehicleDriverName !== 'synthetic') return;
  try {
    await fetch('/api/vehicles/cycle', { method: 'POST' });
  } catch (err) {
    console.error('[vehicles] cycle failed', err);
  }
}

els.vehiclePill.addEventListener('click', cycleVehicle);

const BATTERY_STATE_CLASSES = [
  'battery-safe', 'battery-warn', 'battery-danger',
  'battery-charging', 'battery-unavailable',
];

function applyBatteryState(b) {
  const pill = els.batteryPill;
  const dot = pill.querySelector('.dot');
  const text = pill.querySelector('.pill-text');
  clearClasses(pill, BATTERY_STATE_CLASSES);
  clearClasses(dot, DOT_CLASSES);

  // No battery for active vehicle → hide the pill entirely (don't take topbar space).
  if (!b) {
    pill.style.display = 'none';
    return;
  }
  pill.style.display = '';

  if (!b.available || b.voltage_v == null) {
    pill.classList.add('battery-unavailable');
    dot.classList.add('dot-offline');
    text.textContent = '— V';
    return;
  }

  const v = b.voltage_v;
  const thresholds = currentCalibration?.battery || { warn_low_v: 12.4, danger_low_v: 12.0 };

  if (b.charging) {
    pill.classList.add('battery-charging');
    text.textContent = `${v.toFixed(2)} V · charging`;
    return;
  }

  if (v < thresholds.danger_low_v) {
    pill.classList.add('battery-danger');
    dot.classList.add('dot-danger');
  } else if (v < thresholds.warn_low_v) {
    pill.classList.add('battery-warn');
    dot.classList.add('dot-warn');
  } else {
    pill.classList.add('battery-safe');
    dot.classList.add('dot-online');
  }
  text.textContent = `${v.toFixed(2)} V`;
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
  calib = createCalibration({
    scene,
    panel: els.calibPanel,
    onChange: (cal) => { currentCalibration = cal; },
  });
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

// ─── App-level mode (overview / calibration / diagnostics) ────────
const PAGE_META = {
  overview: { title: 'Overview', subtitle: 'Garage vision & parking assist' },
  calibration: { title: 'Calibration', subtitle: 'Align scene to real garage & vehicle' },
  diagnostics: { title: 'Diagnostics', subtitle: 'System performance & service health' },
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
  if (next === 'diagnostics') startDiagnosticsPolling();
  else stopDiagnosticsPolling();
}

els.nav.addEventListener('click', (e) => {
  const item = e.target.closest('.nav-item');
  if (!item || item.classList.contains('disabled')) return;
  const view = item.dataset.view;
  setAppMode(view);
});

document.addEventListener('app-mode', (e) => setAppMode(e.detail));

// ─── Diagnostics polling ─────────────────────────────
const diagEls = {
  cpu: $('diag-cpu'),
  cpuBar: $('diag-cpu-bar'),
  cpuSub: $('diag-cpu-sub'),
  mem: $('diag-mem'),
  memBar: $('diag-mem-bar'),
  disk: $('diag-disk'),
  diskBar: $('diag-disk-bar'),
  temp: $('diag-temp'),
  tempBar: $('diag-temp-bar'),
  uptime: $('diag-uptime'),
  procmem: $('diag-procmem'),
  sysuptime: $('diag-sysuptime'),
  fps: $('diag-fps'),
  latency: $('diag-latency'),
  close: $('diag-close'),
};

if (diagEls.close) {
  diagEls.close.addEventListener('click', () => setAppMode('overview'));
}

let diagInterval = null;
let diagFpsRaf = null;

function fmtDuration(seconds) {
  if (seconds == null) return '—';
  const s = Math.floor(seconds);
  const days = Math.floor(s / 86400);
  const hours = Math.floor((s % 86400) / 3600);
  const mins = Math.floor((s % 3600) / 60);
  if (days > 0) return `${days}d ${hours}h ${mins}m`;
  if (hours > 0) return `${hours}h ${mins}m`;
  return `${mins}m ${s % 60}s`;
}

function setBar(bar, percent, warnAt = 70, dangerAt = 90) {
  if (!bar) return;
  bar.classList.remove('warn', 'danger');
  if (percent == null) {
    bar.style.width = '0%';
    return;
  }
  bar.style.width = `${Math.min(100, Math.max(0, percent))}%`;
  if (percent >= dangerAt) bar.classList.add('danger');
  else if (percent >= warnAt) bar.classList.add('warn');
}

function applyDiagnostics(d) {
  const cpu = d.cpu || {};
  const mem = d.memory || {};
  const disk = d.disk || {};
  const temp = d.temperature || {};
  const proc = d.process || {};
  const sys = d.system || {};

  diagEls.cpu.textContent = cpu.percent != null ? `${cpu.percent} %` : '— %';
  setBar(diagEls.cpuBar, cpu.percent);
  const load = cpu.load_avg ? `load ${cpu.load_avg.join(' / ')}` : '';
  diagEls.cpuSub.textContent = `${cpu.cores ?? '—'} cores${load ? '  ·  ' + load : ''}`;

  if (mem.total_mb != null) {
    diagEls.mem.textContent = `${mem.used_mb} / ${mem.total_mb} MB`;
    setBar(diagEls.memBar, mem.percent);
  } else {
    diagEls.mem.textContent = '—';
    setBar(diagEls.memBar, null);
  }

  if (disk.total_gb != null) {
    diagEls.disk.textContent = `${disk.used_gb} / ${disk.total_gb} GB`;
    setBar(diagEls.diskBar, disk.percent, 80, 92);
  } else {
    diagEls.disk.textContent = '—';
    setBar(diagEls.diskBar, null);
  }

  if (temp.cpu_c != null) {
    diagEls.temp.textContent = `${temp.cpu_c} °C`;
    // Pi 5 throttles at 80°C; warn at 65°C.
    const pct = Math.min(100, (temp.cpu_c / 85) * 100);
    setBar(diagEls.tempBar, pct, (65 / 85) * 100, (75 / 85) * 100);
  } else {
    diagEls.temp.textContent = '—';
    setBar(diagEls.tempBar, null);
  }

  diagEls.uptime.textContent = fmtDuration(proc.uptime_s);
  diagEls.procmem.textContent = proc.memory_mb != null ? `${proc.memory_mb} MB` : '—';
  diagEls.sysuptime.textContent = fmtDuration(sys.uptime_s);
}

async function pollDiagnostics() {
  try {
    const res = await fetch('/api/diagnostics');
    if (res.ok) applyDiagnostics(await res.json());
  } catch (err) {
    console.error('[diagnostics] poll failed', err);
  }
}

function tickFps() {
  if (appMode !== 'diagnostics') return;
  if (scene && diagEls.fps) {
    diagEls.fps.textContent = `${scene.getFps()} fps`;
  }
  if (els.latency && diagEls.latency) {
    diagEls.latency.textContent = els.latency.textContent;
  }
  diagFpsRaf = requestAnimationFrame(tickFps);
}

function startDiagnosticsPolling() {
  if (diagInterval) return;
  pollDiagnostics();
  diagInterval = setInterval(pollDiagnostics, 2000);
  tickFps();
}

function stopDiagnosticsPolling() {
  if (diagInterval) {
    clearInterval(diagInterval);
    diagInterval = null;
  }
  if (diagFpsRaf) {
    cancelAnimationFrame(diagFpsRaf);
    diagFpsRaf = null;
  }
}

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

  // Door + Lights pills
  applyDoorState(payload.door);
  applyLightsState(payload.lights);

  // Environment pills
  applyEnvironment(payload.environment);

  // Vehicle + battery
  applyVehicleState(payload.vehicles);
  applyBatteryState(payload.battery);

  if (payload.vehicles?.active) {
    currentActiveVehicle = payload.vehicles.active;
    if (scene) scene.applyActiveVehicle(currentActiveVehicle);
  }

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

  ws.onclose = (ev) => {
    setPill(els.serverPill, false, 'Server');
    els.streamStatus.textContent = 'reconnecting';
    // 1008 = policy violation (session invalid). Don't reconnect; show login.
    if (ev && ev.code === 1008) {
      showAuthOverlay({ has_passkeys: true });
      return;
    }
    setTimeout(connect, 1500);
  };

  ws.onerror = () => ws.close();
}

// ─── Auth bootstrap ─────────────────────────────────
async function startApp() {
  try {
    const s = await auth.status();
    if (s.authenticated) {
      hideAuthOverlay();
      connect();
    } else {
      showAuthOverlay(s);
    }
  } catch (err) {
    console.error('[auth] status check failed', err);
    showAuthOverlay({ has_passkeys: false });
  }
}

function showAuthOverlay(state) {
  els.authOverlay.classList.remove('hidden');
  els.authError.textContent = '';

  if (!window.isSecureContext) {
    els.authHeadline.textContent = 'HTTPS REQUIRED';
    els.authSub.textContent =
      'Passkeys only work over HTTPS. You\'re on an insecure URL right now (' +
      window.location.host + '). Open the page at your Tailscale HTTPS hostname instead — usually something like pilot-view.tailXXXX.ts.net.';
    els.authPrimaryBtn.disabled = true;
    els.authPrimaryBtn.textContent = 'Unavailable';
    els.authNicknameRow.classList.add('hidden');
    return;
  }

  if (!auth.isWebAuthnSupported()) {
    els.authHeadline.textContent = 'NOT SUPPORTED';
    els.authSub.textContent =
      "This browser doesn't have the WebAuthn API. Use a modern browser (Chrome, Safari, Firefox, Edge) on this device.";
    els.authPrimaryBtn.disabled = true;
    els.authPrimaryBtn.textContent = 'Unavailable';
    els.authNicknameRow.classList.add('hidden');
    return;
  }

  if (state.has_passkeys) {
    els.authHeadline.textContent = 'SIGN IN';
    els.authSub.textContent = 'Authenticate with your passkey to continue.';
    els.authNicknameRow.classList.add('hidden');
    els.authPrimaryBtn.textContent = 'Use passkey';
    els.authPrimaryBtn.onclick = handleLogin;
    els.authFoot.textContent = '';
  } else {
    els.authHeadline.textContent = 'FIRST DEVICE';
    els.authSub.textContent =
      'No devices are registered yet. This device becomes the trusted master — name it and register a passkey.';
    els.authNicknameRow.classList.remove('hidden');
    els.authPrimaryBtn.textContent = 'Register this device';
    els.authPrimaryBtn.onclick = handleRegister;
    els.authFoot.textContent = 'After this, all future devices must be added from a signed-in one.';
  }
}

function hideAuthOverlay() {
  els.authOverlay.classList.add('hidden');
}

async function handleRegister() {
  const nickname = els.authNicknameInput.value.trim() || 'First device';
  els.authError.textContent = '';
  els.authPrimaryBtn.disabled = true;
  els.authPrimaryBtn.textContent = 'Waiting for passkey…';
  try {
    await auth.register(nickname);
    hideAuthOverlay();
    connect();
  } catch (err) {
    els.authError.textContent = err.message || String(err);
  } finally {
    els.authPrimaryBtn.disabled = false;
    els.authPrimaryBtn.textContent = 'Register this device';
  }
}

async function handleLogin() {
  els.authError.textContent = '';
  els.authPrimaryBtn.disabled = true;
  els.authPrimaryBtn.textContent = 'Waiting for passkey…';
  try {
    await auth.login();
    hideAuthOverlay();
    connect();
  } catch (err) {
    els.authError.textContent = err.message || String(err);
  } finally {
    els.authPrimaryBtn.disabled = false;
    els.authPrimaryBtn.textContent = 'Use passkey';
  }
}

startApp();
