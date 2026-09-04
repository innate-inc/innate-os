/** The sink cabinet is a fixed articulation, not a droppable prop.
 * Dimensions are in the apartment's original Y-up metres. */
import * as T from 'three';
import spec from '../config/cabinet.json';
export { spec as cabinetSpec };

// Subtract an axis-aligned box by retaining the outside of each successive
// plane. Interpolate every vertex attribute so the baked room UVs survive.
export function cutCabinet(geometry: T.BufferGeometry, matrix: T.Matrix4, keepInside = false): T.BufferGeometry {
  const source = geometry.index ? geometry.toNonIndexed() : geometry.clone();
  source.applyMatrix4(matrix);
  const names = Object.keys(source.attributes);
  const sizes = names.map(n => source.getAttribute(n).itemSize);
  const offsets = sizes.map((_, i) => sizes.slice(0, i).reduce((a, b) => a + b, 0));
  const pOffset = offsets[names.indexOf('position')];
  const data: number[][] = names.map(() => []);
  const planes = [0, 1, 2].flatMap(axis => [
    {axis, bound: keepInside && axis === 2 ? spec.hinge[2] - spec.thickness - .001 : spec.cutMin[axis], sign: 1},
    {axis, bound: spec.cutMax[axis], sign: -1},
  ]);
  const emit = (poly: number[][]) => {
    for (let i = 1; i + 1 < poly.length; i++) for (const v of [poly[0], poly[i], poly[i + 1]])
      names.forEach((_, a) => data[a].push(...v.slice(offsets[a], offsets[a] + sizes[a])));
  };
  for (let i = 0; i < source.getAttribute('position').count; i += 3) {
    let poly = [0, 1, 2].map(j => names.flatMap(n => {
      const a = source.getAttribute(n);
      return Array.from({length: a.itemSize}, (_, k) => a.getComponent(i + j, k));
    }));
    for (const {axis, bound, sign} of planes) {
      if (!poly.length) break;
      const inside: number[][] = [], outside: number[][] = [];
      for (let j = 0; j < poly.length; j++) {
        const a = poly[j], b = poly[(j + 1) % poly.length];
        const da = sign * (a[pOffset + axis] - bound), db = sign * (b[pOffset + axis] - bound);
        (da >= 0 ? inside : outside).push(a);
        if ((da >= 0) !== (db >= 0)) {
          const t = da / (da - db), v = a.map((x, k) => x + t * (b[k] - x));
          inside.push(v); outside.push(v);
        }
      }
      if (!keepInside) emit(outside); poly = inside;
    }
    if (keepInside) emit(poly);
  }
  const out = new T.BufferGeometry();
  names.forEach((n, i) => out.setAttribute(n, new T.Float32BufferAttribute(data[i], sizes[i])));
  out.applyMatrix4(matrix.clone().invert()); out.computeBoundingBox(); out.computeBoundingSphere();
  source.dispose(); return out;
}

function extractDoorSurface(geometry: T.BufferGeometry, matrix: T.Matrix4): T.BufferGeometry {
  const surface = cutCabinet(geometry, matrix, true);
  surface.applyMatrix4(matrix);
  surface.translate(-spec.hinge[0], -spec.hinge[1], -spec.hinge[2]);
  return surface;
}

export class KitchenCabinet {
  readonly root = new T.Group();
  readonly door = new T.Group();
  private installed = false;
  constructor() {
    this.root.name = 'kitchen_cabinet'; this.door.name = spec.name;
    const front = new T.MeshStandardMaterial({color: '#827d78', roughness: .85});
    const inner = new T.MeshStandardMaterial({color: '#c5bcb0', roughness: .9});
    const handle = new T.MeshStandardMaterial({color: '#88857f', roughness: .32, metalness: .72});
    const box = (parent: T.Object3D, size: number[], pos: number[], mat: T.Material) => {
      const mesh = new T.Mesh(new T.BoxGeometry(...size as [number, number, number]), mat);
      mesh.position.fromArray(pos);mesh.castShadow = true;mesh.receiveShadow = true;parent.add(mesh);
    };
    const [x,y,z] = spec.hinge, w = spec.width, h = spec.height;
    // Sides, floor and back; no solid collision slab across the opening.
    for (const dx of [0.009, w - .009]) box(this.root,[.018,h,.55],[x+dx,y+h/2,z-.30],inner);
    box(this.root,[w,.018,.55],[x+w/2,y+.009,z-.30],inner);
    box(this.root,[w,h,.018],[x+w/2,y+h/2,z-.566],inner);
    this.door.position.fromArray(spec.hinge); this.root.add(this.door);
    box(this.door,[w,h,spec.thickness],[w/2,h/2,-spec.thickness/2-.0015],front);
    const hz=spec.handleClearance+spec.handleRadius;
    const bar = new T.Mesh(new T.CylinderGeometry(spec.handleRadius,spec.handleRadius,spec.handleLength,16),handle);
    bar.position.set(spec.handleX,spec.handleHeight-y,hz);this.door.add(bar);
    for(const dy of [-spec.handleLength/2,spec.handleLength/2]) {
      const mount=new T.Mesh(new T.CylinderGeometry(spec.handleRadius,spec.handleRadius,hz,16),handle);
      mount.rotation.x=Math.PI/2;mount.position.set(spec.handleX,spec.handleHeight-y+dy,hz/2);this.door.add(mount);
    }
  }
  install(room: T.Object3D): void {
    if(this.installed)return;
    room.updateMatrixWorld(true);
    const meshes: T.Mesh[]=[];
    room.traverse(o=>{if(o instanceof T.Mesh && o.name.includes(spec.room))meshes.push(o)});
    if(!meshes.length)return;
    // The parent already rotates the apartment to Z-up in SimScene. Mesh
    // local transforms alone take vertices to the original room coordinates.
    for(const mesh of meshes){
      mesh.updateMatrix(); const old=mesh.geometry;
      const surface = extractDoorSurface(old, mesh.matrix);
      const panel = new T.Mesh(surface, mesh.material);
      panel.name = 'original_cabinet_finish'; this.door.add(panel);
      mesh.geometry=cutCabinet(old,mesh.matrix);old.dispose();
    }
    room.add(this.root);this.installed=true;
  }
  setAngle(radians: number): void { this.door.rotation.y=-T.MathUtils.clamp(radians,0,T.MathUtils.degToRad(spec.maxAngle)); }
  setPose(poses: Record<string,number[]>): void {
    const p=poses[spec.name]; if(!p)return;
    // MuJoCo Z-up world -> this apartment's Y-up local coordinates.
    this.door.position.set(p[0],p[2],-p[1]);
    this.door.quaternion.set(p[4],p[6],-p[5],p[3]);
  }
}
