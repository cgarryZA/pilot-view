import * as THREE from 'https://esm.sh/three@0.169.0';
import { OrbitControls } from 'https://esm.sh/three@0.169.0/examples/jsm/controls/OrbitControls.js';
import { GLTFLoader } from 'https://esm.sh/three@0.169.0/examples/jsm/loaders/GLTFLoader.js';

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

const CAR_MODEL_URL = '/static/assets/models/gallardo.glb';

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
function buildGarageWalls(w, l, h) {
  const wallDefs = [
    // side    width   height   position                    rotation (axis,deg)        normal (into garage)
    { side: 'rear',    sx: w, sy: h, pos: [0, h/2, 0],     rot: [0, 0, 0],       normal: new THREE.Vector3(0, 0, 1) },
    { side: 'front',   sx: w, sy: h, pos: [0, h/2, l],     rot: [0, Math.PI, 0], normal: new THREE.Vector3(0, 0, -1) },
    { side: 'left',    sx: l, sy: h, pos: [-w/2, h/2, l/2],rot: [0, Math.PI/2, 0], normal: new THREE.Vector3(1, 0, 0) },
    { side: 'right',   sx: l, sy: h, pos: [w/2, h/2, l/2], rot: [0, -Math.PI/2, 0], normal: new THREE.Vector3(-1, 0, 0) },
    { side: 'ceiling', sx: w, sy: l, pos: [0, h, l/2],     rot: [Math.PI/2, 0, 0],  normal: new THREE.Vector3(0, -1, 0) },
    { side: 'floor',   sx: w, sy: l, pos: [0, 0, l/2],     rot: [-Math.PI/2, 0, 0], normal: new THREE.Vector3(0, 1, 0) },
  ];

  const walls = [];
  for (const def of wallDefs) {
    const planeMat = new THREE.MeshBasicMaterial({
      color: COLOR.planeBase,
      transparent: true,
      opacity: 0,                 // hidden by default; orbit mode raises this
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
    walls: [],            // [{side, plane, edges, ...}]
    width: null, length: null, height: null,
  };
  scene.add(garage.group);

  function rebuildGarage(w, l, h) {
    if (garage.width === w && garage.length === l && garage.height === h) return;
    garage.group.clear();
    garage.walls = buildGarageWalls(w, l, h);
    for (const wall of garage.walls) {
      garage.group.add(wall.plane);
      garage.group.add(wall.edges);
    }
    garage.width = w; garage.length = l; garage.height = h;

    // Re-position live camera at the back-wall eye-level looking toward the entrance.
    liveCamera.position.set(0, h * 0.65, l - 0.2);
    liveCamera.lookAt(0, h * 0.28, 0);

    // Update orbit controls target
    controls.target.set(0, h * 0.28, l / 2);
    if (mode === 'orbit') {
      orbitCamera.position.copy(liveCamera.position);
      controls.update();
    }
  }

  // ─── car state ───
  const car = {
    group: new THREE.Group(),
    box: null,          // {group, faces}
    meshGroup: null,    // wireframe edges of GLB
    meshMaterial: new THREE.LineBasicMaterial({ color: COLOR.carMesh, transparent: true, opacity: 0.9 }),
    loaded: false,
    extent: { length: 4.30, width: 1.90, height: 1.16 },
  };
  scene.add(car.group);

  function rebuildCarBox(extent) {
    if (car.box) car.group.remove(car.box.group);
    car.box = buildCarBox(extent);
    car.group.add(car.box.group);
  }
  rebuildCarBox(car.extent);

  // ─── load GLB ───
  const loader = new GLTFLoader();
  loader.load(
    CAR_MODEL_URL,
    (gltf) => {
      const model = gltf.scene;

      // Normalise scale so longest dimension = Gallardo length (4.30m)
      const tempBox = new THREE.Box3().setFromObject(model);
      const size = new THREE.Vector3();
      tempBox.getSize(size);
      const longest = Math.max(size.x, size.y, size.z);
      const scale = 4.30 / longest;
      model.scale.setScalar(scale);

      // Centre on origin, sit on floor
      const scaledBox = new THREE.Box3().setFromObject(model);
      const centre = new THREE.Vector3();
      scaledBox.getCenter(centre);
      model.position.sub(centre);
      const newBox = new THREE.Box3().setFromObject(model);
      const yShift = -newBox.min.y - car.extent.height / 2;
      model.position.y += yShift;
      // Now model sits at y=0..height with centroid at height/2 (matches our car.group convention).

      // Replace materials with wireframe edges
      const wfGroup = new THREE.Group();
      model.traverse((obj) => {
        if (obj.isMesh) {
          const edges = new THREE.EdgesGeometry(obj.geometry, 22);
          const wf = new THREE.LineSegments(edges, car.meshMaterial);
          obj.updateWorldMatrix(true, false);
          wf.applyMatrix4(obj.matrixWorld);
          wfGroup.add(wf);
        }
      });
      car.group.add(wfGroup);
      car.meshGroup = wfGroup;
      car.loaded = true;
      console.info('[scene] gallardo loaded');
    },
    (progress) => {
      if (progress.total) {
        const pct = Math.round((progress.loaded / progress.total) * 100);
        if (pct % 25 === 0) console.info(`[scene] gallardo loading ${pct}%`);
      }
    },
    (err) => console.error('[scene] gallardo failed', err)
  );

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
  function tick() {
    if (!running) return;
    requestAnimationFrame(tick);
    if (mode === 'orbit') controls.update();
    updateWallOpacities();
    renderer.render(scene, activeCamera);
  }
  tick();

  // ─── external API ───
  function applyGeometry(geom) {
    if (!geom) return;

    if (geom.garage) {
      rebuildGarage(geom.garage.width, geom.garage.length, geom.garage.height);
    }
    if (geom.car) {
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

  function destroy() {
    running = false;
    ro.disconnect();
    renderer.dispose();
    renderer.domElement.remove();
  }

  return { applyGeometry, setMode, destroy, get mode() { return mode; } };
}
