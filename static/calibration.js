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
  let activeVehicleId = null;
  // When the user is actively editing, suppress WS-driven scene resets for a
  // short window so the optimistic update isn't clobbered by a still-stale
  // server payload during the save round-trip.
  let editingLockUntil = 0;
  const EDITING_LOCK_MS = 600;

  function bindInputs() {
    inputs = Array.from(panel.querySelectorAll('input[data-path]'));
    for (const input of inputs) {
      const evt = input.type === 'checkbox' ? 'change' : 'input';
      input.addEventListener(evt, () => onInputChanged(input));
    }
  }

  // Form data-paths use "vehicle.X" — translate to the actual location in
  // calibration's vehicles.registry.<active>.X.
  function translatePath(path) {
    if (path.startsWith('vehicle.') && activeVehicleId) {
      return `vehicles.registry.${activeVehicleId}.${path.slice('vehicle.'.length)}`;
    }
    return path;
  }

  function renderValues(cal) {
    current = cal;
    for (const input of inputs) {
      const v = getPath(cal, translatePath(input.dataset.path));
      if (input.type === 'checkbox') {
        input.checked = !!v;
      } else if (typeof v === 'number') {
        if (document.activeElement !== input) {
          input.value = String(v);
        }
      }
    }
    if (scene) scene.applyCalibration(cal);
    pushActiveVehicleToScene(cal);
    if (onChange) onChange(cal);
  }

  function pushActiveVehicleToScene(cal) {
    if (!scene || !activeVehicleId) return;
    const v = cal?.vehicles?.registry?.[activeVehicleId];
    if (v) scene.applyActiveVehicle(v);
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
    setPath(next, translatePath(input.dataset.path), value);
    current = next;

    // Lock out WS-driven scene resets briefly so the optimistic update sticks
    // visually while we wait for the save to round-trip.
    editingLockUntil = Date.now() + EDITING_LOCK_MS;

    if (scene) scene.applyCalibration(current);
    pushActiveVehicleToScene(current);
    if (onChange) onChange(current);
    scheduleSave();
  }

  const scheduleSave = debounce(async () => {
    try {
      const res = await fetch('/api/calibration', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(current),
        credentials: 'same-origin',
      });
      if (res.ok) {
        showSaved();
      } else {
        console.error('[calibration] save returned', res.status);
      }
    } catch (err) {
      console.error('[calibration] save failed', err);
    }
  }, 80);

  function showSaved() {
    savedEl.classList.add('visible');
    clearTimeout(showSaved.timer);
    showSaved.timer = setTimeout(() => savedEl.classList.remove('visible'), 1200);
  }

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

  closeEl.addEventListener('click', () => {
    document.dispatchEvent(new CustomEvent('app-mode', { detail: 'overview' }));
  });

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

  function setActiveVehicle(id) {
    if (id === activeVehicleId) return;
    activeVehicleId = id;
    if (current) renderValues(current);
  }

  bindInputs();
  load();

  function isEditing() {
    return Date.now() < editingLockUntil;
  }

  return {
    reload: load,
    setActiveVehicle,
    current: () => current,
    isEditing,
  };
}
