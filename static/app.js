const $ = (id) => document.getElementById(id);

const els = {
  clock: $('clock'),
  date: $('date'),
  serverPill: $('server-pill'),
  cameraPill: $('camera-pill'),
  stageCard: $('stage-card'),
  camModel: $('cam-model'),
  camIface: $('cam-iface'),
  camSerial: $('cam-serial'),
  camFirmware: $('cam-firmware'),
  camLast: $('cam-last'),
  camError: $('cam-error'),
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

function setPill(pill, online, label) {
  const dot = pill.querySelector('.dot');
  const text = pill.querySelector('.pill-text');
  dot.classList.toggle('dot-online', online);
  dot.classList.toggle('dot-offline', !online);
  pill.classList.toggle('online', online);
  text.textContent = label;
}

function applyState(payload) {
  const c = payload.camera || {};

  setPill(els.serverPill, true, 'Server');
  setPill(els.cameraPill, !!c.connected, c.connected ? 'Camera' : 'Camera');

  els.camModel.textContent = c.model || '—';
  els.camIface.textContent = c.interface || '—';
  els.camSerial.textContent = c.serial || '—';
  els.camFirmware.textContent = c.firmware || '—';
  els.camLast.textContent = fmtTime(c.last_attempt);
  els.camError.textContent = c.error || 'none';

  els.streamStatus.textContent = c.connected ? 'live' : 'idle';
}

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
