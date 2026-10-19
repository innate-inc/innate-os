import * as THREE from "three";

/** World-space emitters, evaluated by the server from the actual danger zones. */
export interface FireState { sources: [number, number, number, number, number][] }
type Point = [number, number, number];
type Triangle = [Point, Point, Point, [number, number, number, number]];

/** Same curved tongues as mars_sim_driver/fire.py; no client hazard clock. */
export function* flameTriangles(source: FireState["sources"][number], t: number): Generator<Triangle> {
  const [x, y, z, strength, seed] = source;
  for (let tongue = 0; tongue < 3; tongue++) {
    const phase = seed + tongue * 2.4;
    const height = 0.89 * Math.sqrt(strength) * (0.80 + 0.20 * Math.sin(t * 8 + phase));
    const radius = 0.175 * Math.sqrt(strength) * (tongue === 0 ? 1 : 0.7);
    const cx = x + Math.cos(phase) * radius * 0.55;
    const cy = y + Math.sin(phase) * radius * 0.55;
    for (let layer = 0; layer < 2; layer++) {
      const h = height * (layer === 0 ? 1 : 0.63), r = radius * (layer === 0 ? 1 : 0.48);
      const color: Triangle[3] = layer === 0 ? [1, 0.18, 0.012, 0.80] : [1, 0.80, 0.18, 0.95];
      for (const angle of [phase, phase + Math.PI / 2]) {
        const ux = Math.cos(angle), uy = Math.sin(angle);
        const points: Point[][] = [];
        for (let level = 0; level < 6; level++) {
          const q = level / 5;
          const width = r * Math.sin(Math.PI * (0.18 + 0.82 * q)) * (1 - q) ** 0.35;
          const sway = h * 0.18 * Math.sin(t * 5 + phase + q * 4) * q * q;
          points.push([-1, 1].map(side => [
            cx + ux * (sway + side * width), cy + uy * (sway + side * width), z + h * q,
          ]));
        }
        for (let i = 0; i < 5; i++) {
          const [a, b] = points[i], [c, d] = points[i + 1];
          yield [a, b, c, color];
          if (i < 4) yield [b, d, c, color];
        }
      }
    }
  }
}

export class FireEffect {
  private group = new THREE.Group();
  private geometry = new THREE.BufferGeometry();
  private positions = new Float32Array(32 * 324 * 3);
  private colors = new Float32Array(32 * 324 * 4);
  private material = new THREE.MeshBasicMaterial({
    vertexColors: true, side: THREE.DoubleSide, transparent: true, depthWrite: false, toneMapped: false,
  });
  private smokeGeometry = new THREE.SphereGeometry(1, 12, 8);
  private smoke: THREE.Mesh<THREE.SphereGeometry, THREE.MeshBasicMaterial>[] = [];
  private glow = new THREE.PointLight(0xff671c, 0, 3, 2);

  constructor(scene: THREE.Scene) {
    this.group.name = "blaze-fire";
    this.group.visible = false;
    this.geometry.setAttribute("position", new THREE.BufferAttribute(this.positions, 3).setUsage(THREE.DynamicDrawUsage));
    this.geometry.setAttribute("color", new THREE.BufferAttribute(this.colors, 4).setUsage(THREE.DynamicDrawUsage));
    this.geometry.setDrawRange(0, 0);
    const mesh = new THREE.Mesh(this.geometry, this.material);
    mesh.frustumCulled = false;
    this.group.add(mesh, this.glow);
    scene.add(this.group);
  }

  update(state: FireState | null, t: number): void {
    this.group.visible = !!state?.sources.length;
    if (!state?.sources.length) return;
    let vertex = 0, puff = 0;
    for (const source of state.sources.slice(0, 32)) {
      for (const [a, b, c, color] of flameTriangles(source, t)) {
        for (const point of [a, b, c]) {
          this.positions.set(point, vertex * 3);
          this.colors.set(color, vertex * 4);
          vertex++;
        }
      }
      const [x, y, z, strength, seed] = source;
      for (let i = 0; i < 4; i++) {
        const age = (t * 0.22 + i / 4 + seed * 0.17) % 1;
        const radius = 0.06 + age * (0.16 + strength * 0.12);
        if (!this.smoke[puff]) {
          const mesh = new THREE.Mesh(this.smokeGeometry, new THREE.MeshBasicMaterial({
            color: 0x211d19, transparent: true, depthWrite: false,
          }));
          this.smoke.push(mesh);
          this.group.add(mesh);
        }
        const mesh = this.smoke[puff++];
        mesh.visible = true;
        mesh.position.set(x + Math.sin(seed + age * 4) * age * 0.16, y + age * 0.10, z + 0.32 + age * 1.12);
        mesh.scale.set(radius, radius, radius * 0.7);
        mesh.material.opacity = Math.sin(Math.PI * age) * (0.08 + strength * 0.12) * Math.min(1, strength * 4);
      }
    }
    for (let i = puff; i < this.smoke.length; i++) this.smoke[i].visible = false;
    this.geometry.setDrawRange(0, vertex);
    this.geometry.getAttribute("position").needsUpdate = true;
    this.geometry.getAttribute("color").needsUpdate = true;
    const [x, y, z, strength] = state.sources[0];
    this.glow.position.set(x, y, z + 0.25);
    this.glow.intensity = strength * (0.7 + 0.15 * Math.sin(t * 11));
  }

  dispose(): void {
    this.group.removeFromParent();
    this.geometry.dispose();
    this.material.dispose();
    this.smokeGeometry.dispose();
    for (const mesh of this.smoke) mesh.material.dispose();
    this.glow.dispose();
  }
}
