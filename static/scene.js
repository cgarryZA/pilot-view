import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { MeshoptDecoder } from 'three/addons/libs/meshopt_decoder.module.js';

const COLOR = {
  carMesh:   0x9bd4ff,
  edgeBase:  0x3a6d8a,
  edgeWarn:  0xffc857,
  edgeDanger:0xff6b6b,
  planeBase: 0x1a2e44,
  planeWarn: 0xffc857,
  planeDanger:0xff6b6b,
  grid:      0x2a4055,
};

const OPACITY = {
  faceBaseEdge: 0.35,
  faceWarnEdge: 1.0,
  facePlaneOrbit: 0.18,
  facePlaneOrbitFaded: 0.04,
  facePlaneWarn: 0.35,
  carBoxBaseEdge: 0.15,
  carBoxWarnEdge: 1.0,
};

// Clearance keys that map to a wall face and a car face.
const SIDES = ['front', 'rear', 'left', 'right', 'ceiling'];

// ─── helpers ────────────────────────────────────────────────────────────
function rectLineSegments(corners, material) {
  // corners: [A, B, C, D] in order around the rectangle
  const pts = [];
  for (let i = 0; i < 4; i++) {
    pts.push(corners[i], corners[(i + 1) % 4]);
  }
  const geo = new THREE.BufferGeometry().setFromPoints(pts);
  return new THREE.LineSegments(geo, material);
}

function classify(value, thresholds) {
  if (value == null) return 'safe';
  if (value < thresholds.danger) return 'danger';
  if (value < thresholds.warn) return 'warning';
  return 'safe';
}

function edgeColorFor(state) {
  return state === 'danger' ? COLOR.edgeDanger
       : state === 'warning' ? COLOR.edgeWarn
       : COLOR.edgeBase;
}

function planeColorFor(state) {
  return state === 'danger' ? COLOR.planeDanger
       : state === 'warning' ? COLOR.planeWarn
       : COLOR.planeBase;
}

// ─── garage walls ───────────────────────────────────────────────────────
// Each wall: { side, plane (Mesh), edges (LineSegments), planeMat, edgeMat, center, normal }
// The entrance wall (rear, z=0) is split into three sections framing the door
// opening; the door panel itself is built separately and animates vertically.
function buildGarageWalls(w, l, h, doorW, doorH) {
  const inwardZ = new THREE.Vector3(0, 0, 1);
  const inwardNegZ = new THREE.Vector3(0, 0, -1);
  const inwardX = new THREE.Vector3(1, 0, 0);
  const inwardNegX = new THREE.Vector3(-1, 0, 0);
  const inwardY = new THREE.Vector3(0, 1, 0);
  const inwardNegY = new THREE.Vector3(0, -1, 0);

  // Clamp the door opening to fit the garage face.
  const dw = Math.min(doorW, w);
  const dh = Math.min(doorH, h);

  // Side strips left and right of the doorway, plus the lintel above it.
  const sideStripW = (w - dw) / 2;
  const lintelH = h - dh;

  // For wall coloring, all three rear sections share the "rear" side label
  // (so the existing clearance-driven highlight still works as a single unit).
  const wallDefs = [
    // ── Rear (entrance) wall, split into 3 panels around the door opening ──
    ...(sideStripW > 0.001 ? [
      { side: 'rear', sx: sideStripW, sy: h, pos: [-(w/2 - sideStripW/2), h/2, 0],
        rot: [0, 0, 0], normal: inwardZ },
      { side: 'rear', sx: sideStripW, sy: h, pos: [w/2 - sideStripW/2, h/2, 0],
        rot: [0, 0, 0], normal: inwardZ },
    ] : []),
    ...(lintelH > 0.001 ? [
      { side: 'rear', sx: dw, sy: lintelH, pos: [0, h - lintelH/2, 0],
        rot: [0, 0, 0], normal: inwardZ },
    ] : []),

    // ── Other walls unchanged ──
    { side: 'front',   sx: w, sy: h, pos: [0, h/2, l],     rot: [0, Math.PI, 0], normal: inwardNegZ },
    { side: 'left',    sx: l, sy: h, pos: [-w/2, h/2, l/2],rot: [0, Math.PI/2, 0], normal: inwardX },
    { side: 'right',   sx: l, sy: h, pos: [w/2, h/2, l/2], rot: [0, -Math.PI/2, 0], normal: inwardNegX },
    { side: 'ceiling', sx: w, sy: l, pos: [0, h, l/2],     rot: [Math.PI/2, 0, 0],  normal: inwardNegY },
    { side: 'floor',   sx: w, sy: l, pos: [0, 0, l/2],     rot: [-Math.PI/2, 0, 0], normal: inwardY },
  ];

  const walls = [];
  for (const def of wallDefs) {
    const planeMat = new THREE.MeshBasicMaterial({
      color: COLOR.planeBase,
      transparent: true,
      opacity: 0,
      side: THREE.DoubleSide,
      depthWrite: false,
    });
    const planeGeo = new THREE.PlaneGeometry(def.sx, def.sy);
    const plane = new THREE.Mesh(planeGeo, planeMat);
    plane.position.fromArray(def.pos);
    plane.rotation.set(def.rot[0], def.rot[1], def.rot[2]);

    const edgeMat = new THREE.LineBasicMaterial({
      color: COLOR.edgeBase,
      transparent: true,
      opacity: OPACITY.faceBaseEdge,
    });
    const edgeGeo = new THREE.EdgesGeometry(planeGeo);
    const edges = new THREE.LineSegments(edgeGeo, edgeMat);
    edges.position.copy(plane.position);
    edges.rotation.copy(plane.rotation);

    walls.push({
      side: def.side,
      plane, planeMat, edges, edgeMat,
      center: plane.position.clone(),
      normal: def.normal,
      state: 'safe',
    });
  }
  return walls;
}

// ─── roller door panel (lives in the entrance opening, slides vertically) ──
function buildRollerDoor(doorW, doorH) {
  const group = new THREE.Group();

  // Frame outline (the panel itself) — a rectangle in the XY plane at z=0.
  const halfW = doorW / 2;
  const frame = new THREE.LineSegments(
    new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(-halfW, 0, 0),     new THREE.Vector3(halfW, 0, 0),
      new THREE.Vector3(halfW, 0, 0),      new THREE.Vector3(halfW, doorH, 0),
      new THREE.Vector3(halfW, doorH, 0),  new THREE.Vector3(-halfW, doorH, 0),
      new THREE.Vector3(-halfW, doorH, 0), new THREE.Vector3(-halfW, 0, 0),
    ]),
    new THREE.LineBasicMaterial({ color: COLOR.edgeBase, transparent: true, opacity: 0.9 })
  );
  group.add(frame);

  // Horizontal slats spaced every ~12cm to suggest a roller door.
  const slatSpacing = 0.12;
  const slatCount = Math.max(2, Math.floor(doorH / slatSpacing) - 1);
  const slatPts = [];
  for (let i = 1; i <= slatCount; i++) {
    const y = (i / (slatCount + 1)) * doorH;
    slatPts.push(new THREE.Vector3(-halfW, y, 0), new THREE.Vector3(halfW, y, 0));
  }
  const slats = new THREE.LineSegments(
    new THREE.BufferGeometry().setFromPoints(slatPts),
    new THREE.LineBasicMaterial({ color: COLOR.edgeBase, transparent: true, opacity: 0.55 })
  );
  group.add(slats);

  return { group, frame, slats, height: doorH, width: doorW };
}

// ─── car bounding box (per-face) ────────────────────────────────────────
function buildCarBox(extent) {
  const halfW = extent.width / 2;
  const halfL = extent.length / 2;
  const halfH = extent.height / 2;

  // Cuboid centred at origin with car-up = +Y, car-front = +Z (toward back wall),
  // car-rear = -Z, car-right = +X, car-left = -X. The car group will be translated
  // so the car sits on the floor with centroid at extent.height/2.
  const V = (x, y, z) => new THREE.Vector3(x, y, z);
  const TFL = V(-halfW,  halfH,  halfL);   // top-front-left
  const TFR = V( halfW,  halfH,  halfL);
  const TRL = V(-halfW,  halfH, -halfL);
  const TRR = V( halfW,  halfH, -halfL);
  const BFL = V(-halfW, -halfH,  halfL);
  const BFR = V( halfW, -halfH,  halfL);
  const BRL = V(-halfW, -halfH, -halfL);
  const BRR = V( halfW, -halfH, -halfL);

  const faceCorners = {
    front:   [TFL, TFR, BFR, BFL],
    rear:    [TRR, TRL, BRL, BRR],
    left:    [TFL, BFL, BRL, TRL],
    right:   [TFR, TRR, BRR, BFR],
    ceiling: [TFL, TRL, TRR, TFR],
  };

  const faces = {};
  const group = new THREE.Group();
  for (const [side, corners] of Object.entries(faceCorners)) {
    const mat = new THREE.LineBasicMaterial({
      color: COLOR.edgeBase,
      transparent: true,
      opacity: OPACITY.carBoxBaseEdge,
    });
    const seg = rectLineSegments(corners, mat);
    group.add(seg);
    faces[side] = { mat, seg, state: 'safe' };
  }
  // sit on the floor (centroid at half-height)
  group.position.y = halfH;
  return { group, faces };
}

// ─── main scene ─────────────────────────────────────────────────────────
export function createScene(container) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setClearColor(0x000000, 0);
  container.appendChild(renderer.domElement);

  const scene = new THREE.Scene();

  // Two cameras: live (fixed) and orbit. Both default to the live viewpoint.
  const liveCamera = new THREE.PerspectiveCamera(50, 1, 0.05, 200);
  const orbitCamera = new THREE.PerspectiveCamera(45, 1, 0.05, 200);
  // Placeholder positions — will be re-positioned when garage dimensions are known.
  liveCamera.position.set(0, 1.45, 5.5);
  liveCamera.lookAt(0, 0.6, 0);
  orbitCamera.position.set(0, 1.45, 5.5);

  const controls = new OrbitControls(orbitCamera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.maxPolarAngle = Math.PI * 0.49;
  controls.minDistance = 1.5;
  controls.maxDistance = 25;

  let mode = 'live';
  let activeCamera = liveCamera;

  function setMode(next) {
    if (next === mode) return;
    mode = next;
    if (next === 'live') {
      activeCamera = liveCamera;
      controls.enabled = false;
      // hide plane fills entirely in live mode
      for (const w of garage.walls) w.planeMat.opacity = 0;
    } else {
      activeCamera = orbitCamera;
      controls.enabled = true;
      // re-position orbit camera to current live camera viewpoint (one-time per mode flip)
      orbitCamera.position.copy(liveCamera.position);
      controls.target.set(0, 0.6, garage.length ? garage.length / 2 : 2.5);
      controls.update();
    }
  }

  // ─── lights (subtle; everything uses LineBasic/MeshBasic so light is mostly cosmetic) ───
  scene.add(new THREE.AmbientLight(0xffffff, 0.9));

  // ─── floor grid (only used in orbit mode) ───
  const grid = new THREE.GridHelper(20, 20, COLOR.grid, COLOR.grid);
  grid.material.transparent = true;
  grid.material.opacity = 0.18;
  grid.visible = false;
  scene.add(grid);

  // ─── garage state ───
  const garage = {
    group: new THREE.Group(),
    walls: [],
    width: null, length: null, height: null,
    doorOpeningWidth: null, doorOpeningHeight: null,
    door: null,   // { group, frame, slats, height, width }
  };
  scene.add(garage.group);

  // Door animation state — interpolates the panel up/down based on payload.door
  const doorAnim = {
    lastStatus: null,
    direction: 0,         // +1 opening, -1 closing, 0 still
    transitionStart: 0,   // performance.now() when transition began
    durationMs: 4000,     // matches TRANSITION_SECONDS in app/door.py
    progress: 0,          // 0 = fully closed, 1 = fully open
  };

  function rebuildGarage(w, l, h, doorW = 2.4, doorH = 2.0) {
    const needsRebuild = (
      garage.width !== w || garage.length !== l || garage.height !== h ||
      garage.doorOpeningWidth !== doorW || garage.doorOpeningHeight !== doorH
    );
    if (!needsRebuild) return;

    garage.group.clear();
    garage.walls = buildGarageWalls(w, l, h, doorW, doorH);
    for (const wall of garage.walls) {
      garage.group.add(wall.plane);
      garage.group.add(wall.edges);
    }

    // Roller door panel sits at z=0 (entrance plane), centred in the X-axis.
    garage.door = buildRollerDoor(doorW, doorH);
    garage.door.group.position.set(0, 0, 0);
    garage.group.add(garage.door.group);

    garage.width = w; garage.length = l; garage.height = h;
    garage.doorOpeningWidth = doorW; garage.doorOpeningHeight = doorH;

    // Orbit camera default target = middle of garage interior (liveCamera position
    // is owned by calibration, see applyCalibration).
    controls.target.set(0, h * 0.28, l / 2);
    if (mode === 'orbit') controls.update();
  }

  // ─── car state ───
  // car.group = positioned by detection data (centred on detected car centroid at z=0)
  //   ├─ car.box.group = bounding box (per-face outlines)
  //   └─ car.meshHolder = sub-group for the GLB; calibration applies to this transform
  //         └─ car.meshGroup = the actual wireframe LineSegments
  const car = {
    group: new THREE.Group(),
    box: null,          // {group, faces}
    meshHolder: new THREE.Group(),
    meshGroup: null,    // wireframe edges of GLB
    meshMaterial: new THREE.LineBasicMaterial({ color: COLOR.carMesh, transparent: true, opacity: 0.9 }),
    loaded: false,
    extent: { length: 4.30, width: 1.90, height: 1.16 },
  };
  scene.add(car.group);
  car.group.add(car.meshHolder);

  function rebuildCarBox(extent) {
    if (car.box) car.group.remove(car.box.group);
    car.box = buildCarBox(extent);
    car.group.add(car.box.group);
  }
  rebuildCarBox(car.extent);

  // ─── GLB loading (swappable per vehicle) ───
  const loader = new GLTFLoader();
  // Decimated GLBs use the EXT_meshopt_compression extension. Register the
  // decoder once so loader.load can decompress them.
  loader.setMeshoptDecoder(MeshoptDecoder);
  let currentModelUrl = null;

  function loadCarModel(url) {
    if (!url || url === currentModelUrl) return;
    currentModelUrl = url;

    // Dispose old mesh
    if (car.meshGroup) {
      car.meshHolder.remove(car.meshGroup);
      car.meshGroup.traverse((obj) => {
        if (obj.geometry) obj.geometry.dispose();
      });
      car.meshGroup = null;
    }
    car.loaded = false;

    loader.load(
      url,
      (gltf) => {
        // Skip if a newer swap has happened in the meantime.
        if (url !== currentModelUrl) return;

        const model = gltf.scene;
        const targetLength = car.extent?.length || 4.30;
        const targetHeight = car.extent?.height || 1.16;

        // Normalise scale so longest dimension = target length
        const tempBox = new THREE.Box3().setFromObject(model);
        const size = new THREE.Vector3();
        tempBox.getSize(size);
        const longest = Math.max(size.x, size.y, size.z);
        const scale = targetLength / longest;
        model.scale.setScalar(scale);

        // Centre on origin, sit so centroid is at targetHeight/2
        const scaledBox = new THREE.Box3().setFromObject(model);
        const centre = new THREE.Vector3();
        scaledBox.getCenter(centre);
        model.position.sub(centre);
        const newBox = new THREE.Box3().setFromObject(model);
        const yShift = -newBox.min.y - targetHeight / 2;
        model.position.y += yShift;

        const wfGroup = new THREE.Group();
        let triangleCount = 0;
        let edgeCount = 0;
        model.traverse((obj) => {
          if (obj.isMesh) {
            if (obj.geometry.index) {
              triangleCount += obj.geometry.index.count / 3;
            } else if (obj.geometry.attributes?.position) {
              triangleCount += obj.geometry.attributes.position.count / 3;
            }
            const edges = new THREE.EdgesGeometry(obj.geometry, 22);
            edgeCount += edges.attributes.position.count / 2;
            const wf = new THREE.LineSegments(edges, car.meshMaterial);
            obj.updateWorldMatrix(true, false);
            wf.applyMatrix4(obj.matrixWorld);
            wfGroup.add(wf);
          }
        });
        car.meshHolder.add(wfGroup);
        car.meshGroup = wfGroup;
        car.loaded = true;
        console.info(`[scene] loaded ${url}: ~${Math.round(triangleCount)} triangles, ~${Math.round(edgeCount)} wireframe edges`);
      },
      (progress) => {
        if (progress.total) {
          const pct = Math.round((progress.loaded / progress.total) * 100);
          if (pct % 25 === 0) console.info(`[scene] model ${url} loading ${pct}%`);
        }
      },
      (err) => console.error(`[scene] model ${url} failed`, err)
    );
  }

  // ─── per-frame wall opacity (orbit mode) ───
  function updateWallOpacities() {
    if (mode !== 'orbit') return;
    const camPos = orbitCamera.position;
    for (const wall of garage.walls) {
      // Vector from camera to wall center.
      const toWall = wall.center.clone().sub(camPos);
      const len = toWall.length();
      if (len < 1e-3) continue;
      toWall.normalize();
      // wall.normal points INTO the garage (inward).
      // If camera looks at wall from outside, (toWall · normal) > 0 → wall is between camera and interior → fade.
      const dot = toWall.dot(wall.normal);
      let target;
      if (dot > 0) {
        // camera outside this wall (relative to interior): fade
        target = OPACITY.facePlaneOrbitFaded;
      } else {
        target = wall.state === 'safe' ? OPACITY.facePlaneOrbit : OPACITY.facePlaneWarn;
      }
      // smooth lerp toward target
      const current = wall.planeMat.opacity;
      wall.planeMat.opacity = current + (target - current) * 0.15;
    }
  }

  // ─── resize ───
  function resize() {
    const { clientWidth, clientHeight } = container;
    renderer.setSize(clientWidth, clientHeight, false);
    const aspect = clientWidth / Math.max(clientHeight, 1);
    liveCamera.aspect = aspect;
    liveCamera.updateProjectionMatrix();
    orbitCamera.aspect = aspect;
    orbitCamera.updateProjectionMatrix();
  }
  resize();
  const ro = new ResizeObserver(resize);
  ro.observe(container);

  // ─── animation loop ───
  let running = true;
  // FPS tracking: simple moving window over the last second of frames.
  const frameTimes = [];
  function tick(now) {
    if (!running) return;
    requestAnimationFrame(tick);

    const t = now || performance.now();
    frameTimes.push(t);
    const cutoff = t - 1000;
    while (frameTimes.length && frameTimes[0] < cutoff) frameTimes.shift();

    if (mode === 'orbit') controls.update();
    updateWallOpacities();
    updateDoorPosition(t);
    renderer.render(scene, activeCamera);
  }
  tick();

  function updateDoorPosition(now) {
    if (!garage.door) return;
    const doorH = garage.doorOpeningHeight || 0;
    let target;
    if (doorAnim.direction !== 0) {
      const elapsed = now - doorAnim.transitionStart;
      const t01 = Math.min(1, Math.max(0, elapsed / doorAnim.durationMs));
      if (doorAnim.direction > 0) doorAnim.progress = t01;
      else                         doorAnim.progress = 1 - t01;
      if (t01 >= 1) doorAnim.direction = 0;  // settled
    }
    // progress 0 = closed (panel at floor), 1 = open (panel up at ceiling).
    garage.door.group.position.y = doorAnim.progress * doorH;
  }

  function getFps() {
    return frameTimes.length;
  }

  // ─── external API ───
  function applyGeometry(geom) {
    if (!geom) return;

    if (geom.garage) {
      rebuildGarage(
        geom.garage.width, geom.garage.length, geom.garage.height,
        geom.garage.door_opening_width, geom.garage.door_opening_height,
      );
    }
    if (geom.car) {
      car.group.visible = true;
      if (geom.car.extent) {
        const e = geom.car.extent;
        if (e.length !== car.extent.length || e.width !== car.extent.width || e.height !== car.extent.height) {
          car.extent = { ...e };
          rebuildCarBox(car.extent);
        }
      }
      const p = geom.car.position;
      car.group.position.set(p.x, 0, p.z);
      car.group.rotation.y = -(geom.car.yaw || 0);
    } else {
      // No car detected (e.g. orbbec RGB-only before depth detection lands).
      car.group.visible = false;
    }

    // Per-side state classification.
    const clearances = geom.clearances || {};
    const thresholds = geom.thresholds || { warn: 0.5, danger: 0.2 };
    const perSide = {};
    for (const k of SIDES) perSide[k] = classify(clearances[k], thresholds);

    // Update car box face colors
    if (car.box) {
      for (const k of SIDES) {
        const face = car.box.faces[k];
        if (!face) continue;
        const state = perSide[k];
        face.mat.color.setHex(edgeColorFor(state));
        face.mat.opacity = state === 'safe' ? OPACITY.carBoxBaseEdge : OPACITY.carBoxWarnEdge;
      }
    }

    // Update wall edge colors + plane colors (opacity handled per-frame in orbit mode)
    for (const wall of garage.walls) {
      const state = perSide[wall.side] || 'safe';
      wall.state = state;
      wall.edgeMat.color.setHex(edgeColorFor(state));
      wall.edgeMat.opacity = state === 'safe' ? OPACITY.faceBaseEdge : OPACITY.faceWarnEdge;
      wall.planeMat.color.setHex(planeColorFor(state));
    }
  }

  function applyActiveVehicle(v) {
    if (!v) return;

    if (v.model_url) loadCarModel(v.model_url);

    if (v.extent) {
      const e = v.extent;
      if (e.length !== car.extent.length || e.width !== car.extent.width || e.height !== car.extent.height) {
        car.extent = { ...e };
        rebuildCarBox(car.extent);
      }
    }

    const off = v.model_offset || { x: 0, y: 0, z: 0 };
    car.meshHolder.position.set(off.x || 0, off.y || 0, off.z || 0);
    const yawDeg = v.model_yaw_deg || 0;
    car.meshHolder.rotation.y = (yawDeg * Math.PI) / 180;
    const s = v.model_scale || 1;
    const sx = v.model_mirror_x ? -s : s;
    car.meshHolder.scale.set(sx, s, s);
  }

  function applyDoor(d) {
    if (!d) return;
    const status = d.status;
    if (status === doorAnim.lastStatus) return;

    const previous = doorAnim.lastStatus;
    doorAnim.lastStatus = status;

    // Snap when status is a terminal state, animate when transitioning.
    if (status === 'closed') {
      doorAnim.direction = 0;
      doorAnim.progress = 0;
    } else if (status === 'open') {
      doorAnim.direction = 0;
      doorAnim.progress = 1;
    } else if (status === 'partial') {
      // Derive direction from the previous status — partial means moving.
      if (previous === 'closed') {
        doorAnim.direction = 1;
        doorAnim.transitionStart = performance.now();
        doorAnim.progress = 0;
      } else if (previous === 'open') {
        doorAnim.direction = -1;
        doorAnim.transitionStart = performance.now();
        doorAnim.progress = 1;
      } else if (previous === null) {
        // First payload reports partial — middle of travel; show as half-open.
        doorAnim.direction = 0;
        doorAnim.progress = 0.5;
      }
    } else if (status === 'fault') {
      // Don't move; whatever position we have, keep.
    }
  }

  function applyCalibration(cal) {
    if (!cal) return;
    // Vehicle-specific things live in applyActiveVehicle.
    // applyCalibration handles scene-wide knobs the calibration panel exposes.

    if (cal.garage) {
      const g = cal.garage;
      if (typeof g.width === 'number' && typeof g.length === 'number' && typeof g.height === 'number') {
        rebuildGarage(g.width, g.length, g.height, g.door_opening_width, g.door_opening_height);
      }
    }

    if (cal.live_view) {
      const lv = cal.live_view;
      if (lv.camera_position) {
        liveCamera.position.set(lv.camera_position.x, lv.camera_position.y, lv.camera_position.z);
      }
      // Set up BEFORE lookAt so the solved roll is honoured (OpenCV→Three.js).
      if (lv.camera_up) {
        liveCamera.up.set(lv.camera_up.x, lv.camera_up.y, lv.camera_up.z);
      }
      if (lv.camera_look_at) {
        liveCamera.lookAt(lv.camera_look_at.x, lv.camera_look_at.y, lv.camera_look_at.z);
      }
      if (lv.camera_fov_deg != null) {
        liveCamera.fov = lv.camera_fov_deg;
        liveCamera.updateProjectionMatrix();
      }
    }
  }

  function destroy() {
    running = false;
    ro.disconnect();
    renderer.dispose();
    renderer.domElement.remove();
  }

  return {
    applyGeometry,
    applyCalibration,
    applyActiveVehicle,
    applyDoor,
    setMode,
    destroy,
    getFps,
    get mode() { return mode; },
  };
}
