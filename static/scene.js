import * as THREE from 'https://esm.sh/three@0.169.0';
import { OrbitControls } from 'https://esm.sh/three@0.169.0/examples/jsm/controls/OrbitControls.js';
import { GLTFLoader } from 'https://esm.sh/three@0.169.0/examples/jsm/loaders/GLTFLoader.js';

const COLOR = {
  garage: 0x62e7ff,
  floor: 0x223043,
  carSafe: 0x4ade80,
  carWarn: 0xffc857,
  carDanger: 0xff6b6b,
  helper: 0x2a4055,
};

const CAR_MODEL_URL = '/static/assets/models/gallardo.glb';

export function createScene(container) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setClearColor(0x000000, 0);
  container.appendChild(renderer.domElement);

  const scene = new THREE.Scene();

  const camera = new THREE.PerspectiveCamera(45, 1, 0.05, 200);
  camera.position.set(7.5, 5.5, 7.5);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.maxPolarAngle = Math.PI * 0.49;
  controls.minDistance = 2;
  controls.maxDistance = 30;

  // ─── Lights (subtle — wireframe uses LineBasicMaterial so light is mainly for any solid bits) ───
  scene.add(new THREE.AmbientLight(0xffffff, 0.8));

  // ─── Floor grid for depth context ───
  const grid = new THREE.GridHelper(20, 20, COLOR.helper, COLOR.helper);
  grid.material.transparent = true;
  grid.material.opacity = 0.25;
  scene.add(grid);

  // ─── Garage wireframe (rebuilt when dimensions change) ───
  const garage = {
    group: new THREE.Group(),
    width: null,
    length: null,
    height: null,
  };
  scene.add(garage.group);

  function buildGarage(w, l, h) {
    if (garage.width === w && garage.length === l && garage.height === h) return;
    garage.group.clear();
    garage.width = w; garage.length = l; garage.height = h;

    // Walls + ceiling: box from (−w/2, 0, 0) to (w/2, h, l)
    const box = new THREE.BoxGeometry(w, h, l);
    const edges = new THREE.EdgesGeometry(box);
    const lines = new THREE.LineSegments(
      edges,
      new THREE.LineBasicMaterial({ color: COLOR.garage, transparent: true, opacity: 0.9 })
    );
    lines.position.set(0, h / 2, l / 2);
    garage.group.add(lines);

    // Floor outline (slightly brighter)
    const floorPts = [
      new THREE.Vector3(-w/2, 0.001, 0),
      new THREE.Vector3( w/2, 0.001, 0),
      new THREE.Vector3( w/2, 0.001, l),
      new THREE.Vector3(-w/2, 0.001, l),
      new THREE.Vector3(-w/2, 0.001, 0),
    ];
    const floorGeo = new THREE.BufferGeometry().setFromPoints(floorPts);
    const floorLine = new THREE.Line(floorGeo, new THREE.LineBasicMaterial({ color: COLOR.garage }));
    garage.group.add(floorLine);
  }

  // ─── Car (placeholder cube + async-loaded GLB wireframe) ───
  const car = {
    group: new THREE.Group(),
    material: new THREE.LineBasicMaterial({ color: COLOR.carSafe }),
    bbox: null,
    mesh: null,
    loaded: false,
  };
  scene.add(car.group);

  // Placeholder cube until GLB loads
  (function placeholder() {
    const ext = { l: 4.3, w: 1.9, h: 1.16 };
    const box = new THREE.BoxGeometry(ext.w, ext.h, ext.l);
    const edges = new THREE.EdgesGeometry(box);
    const lines = new THREE.LineSegments(edges, car.material);
    lines.position.y = ext.h / 2;
    car.bbox = lines;
    car.group.add(lines);
  })();

  // Load the actual model
  const loader = new GLTFLoader();
  loader.load(
    CAR_MODEL_URL,
    (gltf) => {
      const model = gltf.scene;

      // Normalise: many GLBs are huge or rotated wrong. Auto-scale to Gallardo length.
      const tempBox = new THREE.Box3().setFromObject(model);
      const size = new THREE.Vector3();
      tempBox.getSize(size);
      // Use the longest dimension as 'length' and scale uniformly to 4.30m
      const longest = Math.max(size.x, size.y, size.z);
      const scale = 4.30 / longest;
      model.scale.setScalar(scale);

      // Re-measure after scaling and centre on origin, base on floor
      const scaledBox = new THREE.Box3().setFromObject(model);
      const centre = new THREE.Vector3();
      scaledBox.getCenter(centre);
      model.position.sub(centre);                          // centre at origin
      const newBox = new THREE.Box3().setFromObject(model);
      model.position.y -= newBox.min.y;                    // sit on floor

      // Replace materials with wireframe edges
      const wireframeGroup = new THREE.Group();
      model.traverse((obj) => {
        if (obj.isMesh) {
          const edges = new THREE.EdgesGeometry(obj.geometry, 18);
          const wf = new THREE.LineSegments(edges, car.material);
          obj.updateWorldMatrix(true, false);
          wf.applyMatrix4(obj.matrixWorld);
          wireframeGroup.add(wf);
        }
      });

      // Swap placeholder for real model
      car.group.remove(car.bbox);
      car.bbox = null;
      car.mesh = wireframeGroup;
      car.group.add(wireframeGroup);
      car.loaded = true;
      console.info('[scene] gallardo loaded', wireframeGroup);
    },
    (progress) => {
      if (progress.total) {
        const pct = Math.round((progress.loaded / progress.total) * 100);
        console.info(`[scene] gallardo loading ${pct}%`);
      }
    },
    (err) => console.error('[scene] gallardo failed to load', err)
  );

  // ─── Resize ───
  function resize() {
    const { clientWidth, clientHeight } = container;
    renderer.setSize(clientWidth, clientHeight, false);
    camera.aspect = clientWidth / Math.max(clientHeight, 1);
    camera.updateProjectionMatrix();
  }
  resize();
  const ro = new ResizeObserver(resize);
  ro.observe(container);

  // ─── Animation loop ───
  let running = true;
  function tick() {
    if (!running) return;
    requestAnimationFrame(tick);
    controls.update();
    renderer.render(scene, camera);
  }
  tick();

  // ─── External API ───
  function applyGeometry(geom) {
    if (!geom) return;
    if (geom.garage) {
      buildGarage(geom.garage.width, geom.garage.length, geom.garage.height);
    }
    if (geom.car) {
      const p = geom.car.position;
      car.group.position.set(p.x, 0, p.z);
      car.group.rotation.y = -geom.car.yaw || 0;
    }
    if (geom.state) {
      const c = geom.state === 'danger' ? COLOR.carDanger
              : geom.state === 'warning' ? COLOR.carWarn
              : COLOR.carSafe;
      car.material.color.setHex(c);
    }
  }

  function destroy() {
    running = false;
    ro.disconnect();
    renderer.dispose();
    renderer.domElement.remove();
  }

  return { applyGeometry, destroy };
}
