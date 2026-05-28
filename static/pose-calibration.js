import * as THREE from 'https://esm.sh/three@0.169.0';

const $ = (id) => document.getElementById(id);

// Pin labels (display) mapped to door-opening world corners in the order the
// backend expects: bottom-left, bottom-right, top-right, top-left.
const PIN_LABELS = ['BL', 'BR', 'TR', 'TL'];
const SVG_NS = 'http://www.w3.org/2000/svg';

function nsel(name, attrs = {}) {
  const el = document.createElementNS(SVG_NS, name);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

function projectCornersToImage(cal, imgW, imgH) {
  /** Compute initial pin positions by projecting the 4 door-opening world
   * corners with a Three.js camera built from the current calibration. */
  const lv = cal?.live_view || {};
  const garage = cal?.garage || {};
  const doorW = garage.door_opening_width || 2.4;
  const doorH = garage.door_opening_height || 2.0;

  const fov = lv.camera_fov_deg || 50;
  const aspect = imgW / imgH;
  const cam = new THREE.PerspectiveCamera(fov, aspect, 0.05, 200);
  const p = lv.camera_position || { x: 0, y: 1.45, z: 5.6 };
  const t = lv.camera_look_at || { x: 0, y: 0.6, z: 0 };
  cam.position.set(p.x, p.y, p.z);
  cam.lookAt(t.x, t.y, t.z);
  cam.updateMatrixWorld(true);

  const half = doorW / 2;
  const worldCorners = [
    new THREE.Vector3(-half, 0, 0),
    new THREE.Vector3( half, 0, 0),
    new THREE.Vector3( half, doorH, 0),
    new THREE.Vector3(-half, doorH, 0),
  ];

  return worldCorners.map((v) => {
    const projected = v.clone().project(cam);
    // NDC (-1..+1) → pixel (origin top-left, y down)
    const u = (projected.x + 1) / 2 * imgW;
    const v2 = (1 - (projected.y + 1) / 2) * imgH;
    return { x: u, y: v2 };
  });
}

export function createPoseCalibration({ onPoseApplied }) {
  const overlay = $('pose-overlay');
  const stage = $('pose-stage');
  const imgEl = $('pose-image');
  const svg = $('pose-svg');
  const closeBtn = $('pose-close');
  const resetBtn = $('pose-reset');
  const residualEl = $('pose-residual');
  const fovReadoutEl = $('pose-fov-readout');
  const imgSizeEl = $('pose-imgsize');

  let pins = [];                  // [{ x, y }] in image-natural pixels
  let imageSize = { w: 0, h: 0 }; // natural size (pixels)
  let currentCal = null;
  let liveUrl = null;

  function render() {
    if (!imageSize.w || !imageSize.h) return;
    svg.setAttribute('viewBox', `0 0 ${imageSize.w} ${imageSize.h}`);
    sizeSvgToImage();

    // Wipe SVG and re-render
    while (svg.firstChild) svg.removeChild(svg.firstChild);

    if (pins.length === 4) {
      // Outline the polygon connecting pins
      const path = nsel('polygon', {
        class: 'pin-line',
        points: pins.map((p) => `${p.x},${p.y}`).join(' '),
      });
      svg.appendChild(path);
    }

    pins.forEach((p, idx) => {
      const g = nsel('g', { class: 'pin', 'data-idx': idx });
      g.appendChild(nsel('circle', {
        class: 'pin-ring',
        cx: p.x, cy: p.y, r: 22,
      }));
      // small crosshair
      g.appendChild(nsel('line', { class: 'pin-cross', x1: p.x - 7, y1: p.y, x2: p.x + 7, y2: p.y }));
      g.appendChild(nsel('line', { class: 'pin-cross', x1: p.x, y1: p.y - 7, x2: p.x, y2: p.y + 7 }));
      g.appendChild(nsel('text', {
        class: 'pin-label', x: p.x, y: p.y - 28,
      })).textContent = PIN_LABELS[idx];
      g.addEventListener('pointerdown', onPinDown);
      svg.appendChild(g);
    });
  }

  function sizeSvgToImage() {
    // The image is letter-boxed via max-width/max-height. Read its current
    // rendered size and absolutely position the SVG over it.
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
    const startSvgPt = clientToImage(ev.clientX, ev.clientY);
    const start = { ...pins[idx] };
    drag = { idx, g, pointerId: ev.pointerId, startSvgPt, start };
    g.addEventListener('pointermove', onPinMove);
    g.addEventListener('pointerup', onPinUp);
    g.addEventListener('pointercancel', onPinUp);
  }
  function onPinMove(ev) {
    if (!drag) return;
    const pt = clientToImage(ev.clientX, ev.clientY);
    const dx = pt.x - drag.startSvgPt.x;
    const dy = pt.y - drag.startSvgPt.y;
    pins[drag.idx] = {
      x: Math.max(0, Math.min(imageSize.w, drag.start.x + dx)),
      y: Math.max(0, Math.min(imageSize.h, drag.start.y + dy)),
    };
    render();
  }
  function onPinUp(ev) {
    if (!drag) return;
    const g = drag.g;
    g.releasePointerCapture(drag.pointerId);
    g.classList.remove('dragging');
    g.removeEventListener('pointermove', onPinMove);
    g.removeEventListener('pointerup', onPinUp);
    g.removeEventListener('pointercancel', onPinUp);
    drag = null;
    schedulePost();
  }

  function clientToImage(clientX, clientY) {
    const r = imgEl.getBoundingClientRect();
    const x = (clientX - r.left) * (imageSize.w / r.width);
    const y = (clientY - r.top) * (imageSize.h / r.height);
    return { x, y };
  }

  // Debounced solve-pose POST
  let postTimer = null;
  function schedulePost() {
    if (postTimer) clearTimeout(postTimer);
    postTimer = setTimeout(submitPose, 120);
  }

  async function submitPose() {
    if (pins.length !== 4) return;
    const fov = currentCal?.live_view?.camera_fov_deg || 50;
    try {
      const res = await fetch('/api/calibration/solve_pose', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({
          image_points: pins.map((p) => [p.x, p.y]),
          image_size: { width: imageSize.w, height: imageSize.h },
          fov_deg: fov,
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        residualEl.textContent = '— err';
        console.error('[pose] solve failed', body);
        return;
      }
      const data = await res.json();
      residualEl.textContent = `${data.residual_px.toFixed(1)} px`;
      // Reload current calibration so initial-projection logic picks up new pose
      // if the user closes and re-opens. Also notify the rest of the app.
      if (onPoseApplied) onPoseApplied(data);
    } catch (err) {
      residualEl.textContent = '— err';
      console.error('[pose] submit error', err);
    }
  }

  async function open(cal, url) {
    currentCal = cal;
    liveUrl = url;
    if (!url) {
      console.error('[pose] no live image url available');
      return;
    }
    imgEl.src = url;
    await imageLoaded(imgEl);
    imageSize = { w: imgEl.naturalWidth, h: imgEl.naturalHeight };
    imgSizeEl.textContent = `${imageSize.w}×${imageSize.h}`;
    fovReadoutEl.textContent = `${(cal?.live_view?.camera_fov_deg || 50).toFixed(0)}°`;
    pins = projectCornersToImage(cal, imageSize.w, imageSize.h);
    residualEl.textContent = '—';
    overlay.classList.remove('hidden');
    requestAnimationFrame(render);
  }

  function close() {
    overlay.classList.add('hidden');
  }

  function reset() {
    if (!currentCal) return;
    pins = projectCornersToImage(currentCal, imageSize.w, imageSize.h);
    render();
    schedulePost();
  }

  function imageLoaded(img) {
    return new Promise((resolve) => {
      if (img.complete && img.naturalWidth) return resolve();
      img.addEventListener('load', () => resolve(), { once: true });
      img.addEventListener('error', () => resolve(), { once: true });
    });
  }

  closeBtn.addEventListener('click', close);
  resetBtn.addEventListener('click', reset);
  window.addEventListener('resize', () => {
    if (!overlay.classList.contains('hidden')) render();
  });

  return { open, close };
}
