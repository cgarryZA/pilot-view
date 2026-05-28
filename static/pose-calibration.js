import * as THREE from 'https://esm.sh/three@0.169.0';

const $ = (id) => document.getElementById(id);
const SVG_NS = 'http://www.w3.org/2000/svg';

// Node order MUST match the backend's ROOM_ORDER + DOOR_ORDER.
//   room (8): near_tl,tr,br,bl  then  back_tl,tr,br,bl
//   door (4): door_tl,tr,br,bl
// All 12 nodes are draggable. The near corners (placed along the wall seams)
// are what capture an off-centre / off-axis camera in the solve.
const NODES = [
  { key: 'near_tl', label: 'TL', group: 'near', draggable: true },
  { key: 'near_tr', label: 'TR', group: 'near', draggable: true },
  { key: 'near_br', label: 'BR', group: 'near', draggable: true },
  { key: 'near_bl', label: 'BL', group: 'near', draggable: true },
  { key: 'back_tl', label: 'TL', group: 'back', draggable: true },
  { key: 'back_tr', label: 'TR', group: 'back', draggable: true },
  { key: 'back_br', label: 'BR', group: 'back', draggable: true },
  { key: 'back_bl', label: 'BL', group: 'back', draggable: true },
  { key: 'door_tl', label: 'TL', group: 'door', draggable: true },
  { key: 'door_tr', label: 'TR', group: 'door', draggable: true },
  { key: 'door_br', label: 'BR', group: 'door', draggable: true },
  { key: 'door_bl', label: 'BL', group: 'door', draggable: true },
];

// Edges to draw (indices into NODES) — the garage cuboid + door rectangle.
const EDGES = [
  // near rectangle
  [0, 1], [1, 2], [2, 3], [3, 0],
  // back rectangle
  [4, 5], [5, 6], [6, 7], [7, 4],
  // connecting seams (near→back)
  [0, 4], [1, 5], [2, 6], [3, 7],
  // door rectangle
  [8, 9], [9, 10], [10, 11], [11, 8],
];

function nsel(name, attrs = {}) {
  const el = document.createElementNS(SVG_NS, name);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

function worldPoints(cal) {
  const g = cal?.garage || {};
  const W = g.width || 3.0, L = g.length || 5.8, H = g.height || 2.3;
  const dw = g.door_opening_width || 2.4, dh = g.door_opening_height || 2.0;
  const cx = g.door_center_x || 0.0;
  const hw = W / 2, hdw = dw / 2;
  return [
    [-hw, H, L], [hw, H, L], [hw, 0, L], [-hw, 0, L],   // near (z=L)
    [-hw, H, 0], [hw, H, 0], [hw, 0, 0], [-hw, 0, 0],   // back (z=0)
    [cx - hdw, dh, 0], [cx + hdw, dh, 0], [cx + hdw, 0, 0], [cx - hdw, 0, 0],  // door (z=0)
  ];
}

function projectWith(camPos, lookAt, fov, worldPts, imgW, imgH) {
  const cam = new THREE.PerspectiveCamera(fov, imgW / imgH, 0.05, 200);
  cam.position.set(camPos.x, camPos.y, camPos.z);
  cam.lookAt(lookAt.x, lookAt.y, lookAt.z);
  cam.updateMatrixWorld(true);
  return worldPts.map(([x, y, z]) => {
    const v = new THREE.Vector3(x, y, z).project(cam);
    return { x: (v.x + 1) / 2 * imgW, y: (1 - (v.y + 1) / 2) * imgH };
  });
}

function projectAll(cal, imgW, imgH) {
  const lv = cal?.live_view || {};
  const pts = projectWith(
    lv.camera_position || { x: 0, y: 1.45, z: 5.6 },
    lv.camera_look_at || { x: 0, y: 0.6, z: 0 },
    lv.camera_fov_deg || 50,
    worldPoints(cal), imgW, imgH,
  );
  // Clamp to a grabbable on-screen band — a bad initial pose (or near corners
  // that project beyond the frame) would otherwise put pins out of reach.
  const m = 24;
  return pts.map((p) => ({
    x: Math.max(m, Math.min(imgW - m, p.x)),
    y: Math.max(m, Math.min(imgH - m, p.y)),
  }));
}

export function createPoseCalibration({ onPoseApplied }) {
  const overlay = $('pose-overlay');
  const stage = $('pose-stage');
  const imgEl = $('pose-image');
  const svg = $('pose-svg');
  const closeBtn = $('pose-close');
  const applyBtn = $('pose-apply');
  const resetBtn = $('pose-reset');
  const residualEl = $('pose-residual');
  const fovReadoutEl = $('pose-fov-readout');
  const fovSlider = $('pose-fov-slider');
  const imgSizeEl = $('pose-imgsize');

  let pins = [];
  let pinEls = [];
  let edgeEls = [];
  let imageSize = { w: 0, h: 0 };
  let currentCal = null;
  let currentFov = 50;

  function render() {
    if (!imageSize.w || !imageSize.h) return;
    svg.setAttribute('viewBox', `0 0 ${imageSize.w} ${imageSize.h}`);
    sizeSvgToImage();
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    pinEls = [];
    edgeEls = [];

    // Edges first (under the pins)
    EDGES.forEach(([a, b]) => {
      const grp = NODES[b].group === 'door' || NODES[a].group === 'door' ? 'door'
                : (NODES[a].group === 'back' && NODES[b].group === 'back') ? 'back'
                : 'near';
      const line = nsel('line', { class: `pin-edge pin-edge--${grp}` });
      svg.appendChild(line);
      edgeEls.push({ line, a, b });
    });

    NODES.forEach((node, idx) => {
      const cls = `pin pin--${node.group}${node.draggable ? '' : ' pin--guide'}`;
      const g = nsel('g', { class: cls, 'data-idx': idx });
      const ring = nsel('circle', { class: 'pin-ring', r: node.draggable ? 18 : 12 });
      const crossH = nsel('line', { class: 'pin-cross' });
      const crossV = nsel('line', { class: 'pin-cross' });
      const label = nsel('text', { class: 'pin-label' });
      label.textContent = node.label;
      g.append(ring, crossH, crossV, label);
      if (node.draggable) g.addEventListener('pointerdown', onPinDown);
      svg.appendChild(g);
      pinEls.push({ g, ring, crossH, crossV, label });
    });
    updatePinPositions();
  }

  function updatePinPositions() {
    edgeEls.forEach(({ line, a, b }) => {
      line.setAttribute('x1', pins[a].x); line.setAttribute('y1', pins[a].y);
      line.setAttribute('x2', pins[b].x); line.setAttribute('y2', pins[b].y);
    });
    pins.forEach((p, idx) => {
      const e = pinEls[idx];
      if (!e) return;
      e.ring.setAttribute('cx', p.x); e.ring.setAttribute('cy', p.y);
      e.crossH.setAttribute('x1', p.x - 6); e.crossH.setAttribute('y1', p.y);
      e.crossH.setAttribute('x2', p.x + 6); e.crossH.setAttribute('y2', p.y);
      e.crossV.setAttribute('x1', p.x); e.crossV.setAttribute('y1', p.y - 6);
      e.crossV.setAttribute('x2', p.x); e.crossV.setAttribute('y2', p.y + 6);
      e.label.setAttribute('x', p.x); e.label.setAttribute('y', p.y - 24);
    });
  }

  function sizeSvgToImage() {
    const r = imgEl.getBoundingClientRect();
    const stageRect = stage.getBoundingClientRect();
    svg.style.width = `${r.width}px`;
    svg.style.height = `${r.height}px`;
    svg.style.left = `${r.left - stageRect.left}px`;
    svg.style.top = `${r.top - stageRect.top}px`;
  }

  let drag = null;
  function onPinDown(ev) {
    ev.preventDefault();
    const g = ev.currentTarget;
    const idx = parseInt(g.dataset.idx, 10);
    g.classList.add('dragging');
    g.setPointerCapture(ev.pointerId);
    drag = { idx, g, pointerId: ev.pointerId, startPt: clientToImage(ev.clientX, ev.clientY), start: { ...pins[idx] } };
    g.addEventListener('pointermove', onPinMove);
    g.addEventListener('pointerup', onPinUp);
    g.addEventListener('pointercancel', onPinUp);
  }
  function onPinMove(ev) {
    if (!drag) return;
    const pt = clientToImage(ev.clientX, ev.clientY);
    pins[drag.idx] = {
      x: Math.max(0, Math.min(imageSize.w, drag.start.x + (pt.x - drag.startPt.x))),
      y: Math.max(0, Math.min(imageSize.h, drag.start.y + (pt.y - drag.startPt.y))),
    };
    updatePinPositions();
  }
  function onPinUp() {
    if (!drag) return;
    const g = drag.g;
    g.releasePointerCapture(drag.pointerId);
    g.classList.remove('dragging');
    g.removeEventListener('pointermove', onPinMove);
    g.removeEventListener('pointerup', onPinUp);
    g.removeEventListener('pointercancel', onPinUp);
    drag = null;
    // Preview the residual (doesn't move pins or save) so you get instant
    // feedback on fit quality without your placement being overwritten.
    previewResidual();
  }

  let previewTimer = null;
  function previewResidual() {
    if (previewTimer) clearTimeout(previewTimer);
    previewTimer = setTimeout(async () => {
      try {
        const res = await fetch('/api/calibration/solve_pose', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'same-origin',
          body: JSON.stringify({
            room_points: pins.slice(0, 8).map((p) => [p.x, p.y]),
            door_points: pins.slice(8, 12).map((p) => [p.x, p.y]),
            image_size: { width: imageSize.w, height: imageSize.h },
            fov_deg: currentFov,
            apply: false,
          }),
        });
        if (!res.ok) { residualEl.textContent = 'error'; return; }
        const data = await res.json();
        residualEl.textContent = `${data.residual_px.toFixed(1)} px`;
      } catch {
        residualEl.textContent = 'error';
      }
    }, 100);
  }

  function setFov(v, { persist = false } = {}) {
    currentFov = v;
    fovReadoutEl.textContent = `${v}°`;
    if (fovSlider.value !== String(v)) fovSlider.value = String(v);
    previewResidual();
    if (persist) {
      fetch('/api/calibration', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ live_view: { camera_fov_deg: v } }),
      }).catch((err) => console.error('[pose] fov persist failed', err));
      if (currentCal?.live_view) currentCal.live_view.camera_fov_deg = v;
    }
  }

  function clientToImage(clientX, clientY) {
    const r = imgEl.getBoundingClientRect();
    return {
      x: (clientX - r.left) * (imageSize.w / r.width),
      y: (clientY - r.top) * (imageSize.h / r.height),
    };
  }

  async function apply() {
    applyBtn.disabled = true;
    applyBtn.textContent = 'Solving…';
    try {
      const res = await fetch('/api/calibration/solve_pose', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({
          room_points: pins.slice(0, 8).map((p) => [p.x, p.y]),
          door_points: pins.slice(8, 12).map((p) => [p.x, p.y]),
          image_size: { width: imageSize.w, height: imageSize.h },
          fov_deg: currentFov,
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        residualEl.textContent = 'error';
        console.error('[pose] solve failed', body);
        applyBtn.disabled = false;
        applyBtn.textContent = 'Apply';
        return;
      }
      const data = await res.json();
      residualEl.textContent = `${data.residual_px.toFixed(1)} px`;
      if (onPoseApplied) onPoseApplied(data);
      close();
    } catch (err) {
      residualEl.textContent = 'error';
      console.error('[pose] apply error', err);
    } finally {
      applyBtn.disabled = false;
      applyBtn.textContent = 'Apply';
    }
  }

  async function open(cal, url) {
    currentCal = cal;
    if (!url) { console.error('[pose] no live image url'); return; }
    imgEl.src = url;
    await imageLoaded(imgEl);
    imageSize = { w: imgEl.naturalWidth, h: imgEl.naturalHeight };
    imgSizeEl.textContent = `${imageSize.w}×${imageSize.h}`;
    currentFov = Math.round(cal?.live_view?.camera_fov_deg || 50);
    fovSlider.value = String(currentFov);
    fovReadoutEl.textContent = `${currentFov}°`;
    pins = projectAll(cal, imageSize.w, imageSize.h);
    residualEl.textContent = '—';
    overlay.classList.remove('hidden');
    requestAnimationFrame(render);
  }

  function close() { overlay.classList.add('hidden'); }

  function reset() {
    if (!currentCal) return;
    pins = projectAll(currentCal, imageSize.w, imageSize.h);
    updatePinPositions();
  }

  function imageLoaded(img) {
    return new Promise((resolve) => {
      if (img.complete && img.naturalWidth) return resolve();
      img.addEventListener('load', () => resolve(), { once: true });
      img.addEventListener('error', () => resolve(), { once: true });
    });
  }

  closeBtn.addEventListener('click', close);   // X = cancel
  applyBtn.addEventListener('click', apply);   // Apply = solve + close
  resetBtn.addEventListener('click', reset);
  // FOV: live residual update while dragging, persist on release.
  fovSlider.addEventListener('input', () => setFov(parseInt(fovSlider.value, 10)));
  fovSlider.addEventListener('change', () => setFov(parseInt(fovSlider.value, 10), { persist: true }));
  window.addEventListener('resize', () => {
    if (!overlay.classList.contains('hidden')) render();
  });

  return { open, close };
}
