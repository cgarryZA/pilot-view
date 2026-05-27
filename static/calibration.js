const $ = (id) => document.getElementById(id);

function getPath(obj, path) {
  return path.split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj);
}

function setPath(obj, path, value) {
  const parts = path.split('.');
  let cur = obj;
  for (let i = 0; i < parts.length - 1; i++) {
    if (cur[parts[i]] == null || typeof cur[parts[i]] !== 'object') {
      cur[parts[i]] = {};
    }
    cur = cur[parts[i]];
  }
  cur[parts[parts.length - 1]] = value;
}

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

export function createCalibration({ scene, panel, onChange }) {
  const tabsEl = $('calib-tabs');
  const bodyEl = $('calib-body');
  const closeEl = $('calib-close');
  const resetEl = $('calib-reset');
  const savedEl = $('calib-saved');

  let current = null;
  let inputs = [];

  function bindInputs() {
    inputs = Array.from(panel.querySelectorAll('input[data-path]'));
    for (const input of inputs) {
      const evt = input.type === 'checkbox' ? 'change' : 'input';
      input.addEventListener(evt, () => onInputChanged(input));
    }
  }

  function renderValues(cal) {
    current = cal;
    for (const input of inputs) {
      const v = getPath(cal, input.dataset.path);
      if (input.type === 'checkbox') {
        input.checked = !!v;
      } else if (typeof v === 'number') {
        // Avoid clobbering the input while the user is typing.
        if (document.activeElement !== input) {
          input.value = String(v);
        }
      }
    }
    if (scene) scene.applyCalibration(cal);
    if (onChange) onChange(cal);
  }

  function onInputChanged(input) {
    if (!current) return;

    let value;
    if (input.type === 'checkbox') {
      value = input.checked;
    } else {
      const raw = input.value;
      if (raw === '' || raw === '-') return;
      const num = parseFloat(raw);
      if (!Number.isFinite(num)) return;
      value = num;
    }

    const next = JSON.parse(JSON.stringify(current));
    setPath(next, input.dataset.path, value);
    current = next;

    if (scene) scene.applyCalibration(current);
    if (onChange) onChange(current);
    scheduleSave();
  }

  const scheduleSave = debounce(async () => {
    try {
      const res = await fetch('/api/calibration', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(current),
      });
      if (res.ok) showSaved();
    } catch (err) {
      console.error('[calibration] save failed', err);
    }
  }, 200);

  function showSaved() {
    savedEl.classList.add('visible');
    clearTimeout(showSaved.timer);
    showSaved.timer = setTimeout(() => savedEl.classList.remove('visible'), 1200);
  }

  // ─── Tab switching ───
  tabsEl.addEventListener('click', (e) => {
    const tab = e.target.closest('.calib-tab');
    if (!tab) return;
    for (const t of tabsEl.querySelectorAll('.calib-tab')) {
      t.classList.toggle('active', t === tab);
    }
    const targetTab = tab.dataset.tab;
    for (const s of bodyEl.querySelectorAll('.calib-section')) {
      s.classList.toggle('active', s.dataset.tab === targetTab);
    }
  });

  // ─── Close button → leave calibration mode ───
  closeEl.addEventListener('click', () => {
    document.dispatchEvent(new CustomEvent('app-mode', { detail: 'overview' }));
  });

  // ─── Reset to defaults ───
  resetEl.addEventListener('click', async () => {
    try {
      const res = await fetch('/api/calibration/reset', { method: 'POST' });
      if (res.ok) {
        const cal = await res.json();
        renderValues(cal);
        showSaved();
      }
    } catch (err) {
      console.error('[calibration] reset failed', err);
    }
  });

  // ─── Initial load ───
  async function load() {
    try {
      const res = await fetch('/api/calibration');
      if (res.ok) {
        const cal = await res.json();
        renderValues(cal);
      }
    } catch (err) {
      console.error('[calibration] load failed', err);
    }
  }

  bindInputs();
  load();

  return { reload: load, current: () => current };
}
