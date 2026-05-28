import * as THREE from 'https://esm.sh/three@0.169.0';

const $ = (id) => document.getElementById(id);
const SVG_NS = 'http://www.w3.org/2000/svg';

// 12 nodes: 4 back-wall corners (blue, exact), 4 near direction nodes (white,
// direction-only), 4 door corners (yellow, exact). back[i] pairs with near[i].
const NODES = [
  { label: 'TL', group: 'back' }, { label: 'TR', group: 'back' },
  { label: 'BR', group: 'back' }, { label: 'BL', group: 'back' },
  { label: 'TL', group: 'near' }, { label: 'TR', group: 'near' },
  { label: 'BR', group: 'near' }, { label: 'BL', group: 'near' },
  { label: 'TL', group: 'door' }, { label: 'TR', group: 'door' },
  { label: 'BR', group: 'door' }, { label: 'BL', group: 'door' },
];

// [a, b, group] — edges drawn between node indices.
const EDGES = [
  [0, 1, 'back'], [1, 2, 'back'], [2, 3, 'back'], [3, 0, 'back'],   // back rectangle
  [0, 4, 'near'], [1, 5, 'near'], [2, 6, 'near'], [3, 7, 'near'],   // receding seams
  [8, 9, 'door'], [9, 10, 'door'], [10, 11, 'door'], [11, 8, 'door'], // door rectangle
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
    [-hw, H, 0], [hw, H, 0], [hw, 0, 0], [-hw, 0, 0],          // back z=0
    [-hw, H, L], [hw, H, L], [hw, 0, L], [-hw, 0, L],          // near z=L
    [cx - hdw, dh, 0], [cx + hdw, dh, 0], [cx + hdw, 0, 0], [cx - hdw, 0, 0], // door
  ];
}

function projectWith(pose, fov, worldPts, imgW, imgH) {
  const cam = new THREE.PerspectiveCamera(fov, imgW / imgH, 0.05, 200);
  const up = pose.camera_up || { x: 0, y: 1, z: 0 };
  cam.up.set(up.x, up.y, up.z);
  cam.position.set(pose.camera_position.x, pose.camera_position.y, pose.camera_position.z);
  cam.lookAt(pose.camera_look_at.x, pose.camera_look_at.y, pose.camera_look_at.z);
  cam.updateMatrixWorld(true);
  return worldPts.map(([x, y, z]) => {
    const v = new THREE.Vector3(x, y, z).project(cam);
    return { x: (v.x + 1) / 2 * imgW, y: (1 - (v.y + 1) / 2) * imgH };
  });
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
  const imgSizeEl = $('pose-imgsize');

  let pins = [];
  let pinEls = [];
  let edgeEls = [];
  let solvedEdgeEls = [];
  let imageSize = { w: 0, h: 0 };
  let currentCal = null;
  let knownFov = null;   // set when the real camera reports its intrinsics

  function render() {
    if (!imageSize.w || !imageSize.h) return;
    svg.setAttribute('viewBox', `0 0 ${imageSize.w} ${imageSize.h}`);
    sizeSvgToImage();
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    pinEls = []; edgeEls = []; solvedEdgeEls = [];

    // Faint "solved" box (drawn first, under everything)
    EDGES.forEach(() => {
      const line = nsel('line', { class: 'pin-edge pin-edge--solved' });
      svg.appendChild(line);
      solvedEdgeEls.push(line);
    });
    // User-pin edges
    EDGES.forEach(([, , grp]) => {
      const line = nsel('line', { class: `pin-edge pin-edge--${grp}` });
      svg.appendChild(line);
      edgeEls.push(line);
    });
    // Pins
    NODES.forEach((node, idx) => {
      const g = nsel('g', { class: `pin pin--${node.group}`, 'data-idx': idx });
      const ring = nsel('circle', { class: 'pin-ring', r: 16 });
      const crossH = nsel('line', { class: 'pin-cross' });
      const crossV = nsel('line', { class: 'pin-cross' });
      const label = nsel('text', { class: 'pin-label' });
      label.textContent = node.label;
      g.append(ring, crossH, crossV, label);
      g.addEventListener('pointerdown', onPinDown);
      svg.appendChild(g);
      pinEls.push({ g, ring, crossH, crossV, label });
    });
    updatePinPositions();
  }

  function updatePinPositions() {
    EDGES.forEach(([a, b], i) => {
      const e = edgeEls[i];
      e.setAttribute('x1', pins[a].x); e.setAttribute('y1', pins[a].y);
      e.setAttribute('x2', pins[b].x); e.setAttribute('y2', pins[b].y);
    });
    pins.forEach((p, idx) => {
      const e = pinEls[idx];
      if (!e) return;
      e.ring.setAttribute('cx', p.x); e.ring.setAttribute('cy', p.y);
      e.crossH.setAttribute('x1', p.x - 6); e.crossH.setAttribute('y1', p.y);
      e.crossH.setAttribute('x2', p.x + 6); e.crossH.setAttribute('y2', p.y);
      e.crossV.setAttribute('x1', p.x); e.crossV.setAttribute('y1', p.y - 6);
      e.crossV.setAttribute('x2', p.x); e.crossV.setAttribute('y2', p.y + 6);
      e.label.setAttribute('x', p.x); e.label.setAttribute('y', p.y - 22);
    });
  }

  function drawSolved(pose) {
    const proj = projectWith(pose, pose.camera_fov_deg, worldPoints(currentCal), imageSize.w, imageSize.h);
    EDGES.forEach(([a, b], i) => {
      const e = solvedEdgeEls[i];
      e.setAttribute('x1', proj[a].x); e.setAttribute('y1', proj[a].y);
      e.setAttribute('x2', proj[b].x); e.setAttribute('y2', proj[b].y);
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
    preview();
  }

  function clientToImage(clientX, clientY) {
    const r = imgEl.getBoundingClientRect();
    return {
      x: (clientX - r.left) * (imageSize.w / r.width),
      y: (clientY - r.top) * (imageSize.h / r.height),
    };
  }

  function solveBody(apply) {
    return {
      back_points: pins.slice(0, 4).map((p) => [p.x, p.y]),
      near_points: pins.slice(4, 8).map((p) => [p.x, p.y]),
      door_points: pins.slice(8, 12).map((p) => [p.x, p.y]),
      image_size: { width: imageSize.w, height: imageSize.h },
      known_fov_deg: knownFov || undefined,
      apply,
    };
  }

  let previewTimer = null;
  function preview() {
    if (previewTimer) clearTimeout(previewTimer);
    previewTimer = setTimeout(async () => {
      try {
        const res = await fetch('/api/calibration/solve_pose', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'same-origin',
          body: JSON.stringify(solveBody(false)),
        });
        if (!res.ok) { residualEl.textContent = 'error'; return; }
        const data = await res.json();
        residualEl.textContent = `${data.residual_px.toFixed(1)} px`;
        fovReadoutEl.textContent = `${data.camera_fov_deg.toFixed(0)}°${data.fov_solved ? ' (solved)' : ' (camera)'}`;
        drawSolved(data);
      } catch {
        residualEl.textContent = 'error';
      }
    }, 100);
  }

  async function apply() {
    applyBtn.disabled = true;
    applyBtn.textContent = 'Solving…';
    try {
      const res = await fetch('/api/calibration/solve_pose', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify(solveBody(true)),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        residualEl.textContent = 'error';
        console.error('[pose] solve failed', body);
        return;
      }
      const data = await res.json();
      if (onPoseApplied) onPoseApplied(data);
      close();
    } catch (err) {
      console.error('[pose] apply error', err);
    } finally {
      applyBtn.disabled = false;
      applyBtn.textContent = 'Apply';
    }
  }

  async function open(cal, url, cameraFov = null) {
    currentCal = cal;
    knownFov = cameraFov && cameraFov > 0 ? cameraFov : null;
    if (!url) { console.error('[pose] no live image url'); return; }
    imgEl.src = url;
    await imageLoaded(imgEl);
    imageSize = { w: imgEl.naturalWidth, h: imgEl.naturalHeight };
    imgSizeEl.textContent = `${imageSize.w}×${imageSize.h}`;
    fovReadoutEl.textContent = knownFov ? `${knownFov.toFixed(0)}° (camera)` : '—';
    pins = initialPins(cal);
    residualEl.textContent = '—';
    overlay.classList.remove('hidden');
    requestAnimationFrame(render);
  }

  function initialPins(cal) {
    const lv = cal?.live_view || {};
    const pose = {
      camera_position: lv.camera_position || { x: 0, y: 1.45, z: 5.6 },
      camera_look_at: lv.camera_look_at || { x: 0, y: 0.6, z: 0 },
      camera_up: lv.camera_up || { x: 0, y: 1, z: 0 },
    };
    const proj = projectWith(pose, lv.camera_fov_deg || 50, worldPoints(cal), imageSize.w, imageSize.h);
    const m = 24;
    return proj.map((p) => ({
      x: Math.max(m, Math.min(imageSize.w - m, p.x)),
      y: Math.max(m, Math.min(imageSize.h - m, p.y)),
    }));
  }

  function close() { overlay.classList.add('hidden'); }
  function reset() { if (currentCal) { pins = initialPins(currentCal); updatePinPositions(); } }

  function imageLoaded(img) {
    return new Promise((resolve) => {
      if (img.complete && img.naturalWidth) return resolve();
      img.addEventListener('load', () => resolve(), { once: true });
      img.addEventListener('error', () => resolve(), { once: true });
    });
  }

  closeBtn.addEventListener('click', close);
  applyBtn.addEventListener('click', apply);
  resetBtn.addEventListener('click', reset);
  window.addEventListener('resize', () => { if (!overlay.classList.contains('hidden')) render(); });

  return { open, close };
}
