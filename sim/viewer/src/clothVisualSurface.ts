import * as THREE from "three";

let cottonBump: THREE.DataTexture | undefined;
let woolBump: THREE.DataTexture | undefined;

export function woolBumpTexture(): THREE.DataTexture {
  if (!woolBump) {
    woolBump = cottonBumpTexture().clone();
    woolBump.repeat.set(3, 6);
    woolBump.needsUpdate = true;
  }
  return woolBump;
}

/** Subtle yarn-scale relief only: no displacement or extra collision geometry. */
export function cottonBumpTexture(): THREE.DataTexture {
  if (cottonBump) return cottonBump;
  const size = 256;
  const data = new Uint8Array(size * size);
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const u = (x % 16) / 16;
      const v = (y % 16) / 16;
      const yarn = Math.cos(2 * Math.PI * (v - Math.abs(2 * u - 1) * 0.45));
      data[y * size + x] = Math.round(128 + 55 * yarn);
    }
  }
  cottonBump = new THREE.DataTexture(data, size, size, THREE.RedFormat);
  cottonBump.wrapS = cottonBump.wrapT = THREE.RepeatWrapping;
  cottonBump.repeat.set(12, 24);
  cottonBump.magFilter = THREE.LinearFilter;
  cottonBump.minFilter = THREE.LinearMipmapLinearFilter;
  cottonBump.generateMipmaps = true;
  cottonBump.needsUpdate = true;
  return cottonBump;
}

type Stencil = Map<number, number>;

/** Topological Loop subdivision, visual only. Never weld by spatial proximity:
 * opposite sides of a fold must remain separate even when they touch.
 * Stencils are compiled once; each streamed frame only applies sparse weights.
 */
export class ClothVisualSurface {
  readonly geometry = new THREE.BufferGeometry();
  readonly controlGeometry: THREE.BufferGeometry;
  private readonly stencils: Stencil[];
  private readonly normalOffset: number;
  private readonly midSurface: THREE.BufferGeometry;

  constructor(controlGeometry: THREE.BufferGeometry, levels = 2, normalOffset = 0) {
    if (!Number.isFinite(normalOffset) || normalOffset < 0 || normalOffset > 0.002) {
      throw new Error("Visual cloth offset must be between zero and 2 mm");
    }
    this.normalOffset = normalOffset;
    this.midSurface = normalOffset ? new THREE.BufferGeometry() : this.geometry;
    this.controlGeometry = controlGeometry;
    const positions = controlGeometry.getAttribute("position");
    const index = controlGeometry.index;
    if (
      !index ||
      !positions ||
      levels < 0 ||
      levels > 2 ||
      !Number.isInteger(levels)
    ) {
      throw new Error(
        "Cloth surface requires indexed triangles and 0–2 subdivision levels",
      );
    }
    let faces = Array.from(index.array);
    if (
      faces.length % 3 ||
      faces.some((i) => !Number.isInteger(i) || i < 0 || i >= positions.count)
    ) {
      throw new Error("Invalid cloth triangle indices");
    }
    for (let i = 0; i < faces.length; i += 3) {
      if (new Set(faces.slice(i, i + 3)).size !== 3)
        throw new Error("Degenerate cloth triangle");
    }
    let weights: Stencil[] = Array.from(
      { length: positions.count },
      (_, i) => new Map([[i, 1]]),
    );
    const mix = (parts: [Stencil, number][]): Stencil => {
      const result: Stencil = new Map();
      for (const [part, scale] of parts)
        for (const [i, w] of part)
          result.set(i, (result.get(i) ?? 0) + w * scale);
      return result;
    };
    for (let level = 0; level < levels; level++) {
      const edges = new Map<
        string,
        { a: number; b: number; opposite: number[]; id: number }
      >();
      const neighbors = weights.map(() => new Set<number>());
      const boundary = weights.map(() => new Set<number>());
      const key = (a: number, b: number) => (a < b ? `${a}:${b}` : `${b}:${a}`);
      for (let f = 0; f < faces.length; f += 3) {
        const tri = faces.slice(f, f + 3);
        for (let j = 0; j < 3; j++) {
          const [a, b, c] = [tri[j], tri[(j + 1) % 3], tri[(j + 2) % 3]];
          neighbors[a].add(b);
          neighbors[b].add(a);
          const k = key(a, b);
          if (!edges.has(k))
            edges.set(k, {
              a,
              b,
              opposite: [],
              id: weights.length + edges.size,
            });
          edges.get(k)!.opposite.push(c);
        }
      }
      for (const e of edges.values()) {
        if (e.opposite.length > 2)
          throw new Error("Non-manifold cloth surface");
        if (e.opposite.length === 1) {
          boundary[e.a].add(e.b);
          boundary[e.b].add(e.a);
        }
      }
      const next = weights.map((w, i) => {
        const rim = [...boundary[i]];
        if (rim.length === 2)
          return mix([
            [w, 0.75],
            [weights[rim[0]], 0.125],
            [weights[rim[1]], 0.125],
          ]);
        if (rim.length) throw new Error("Non-manifold cloth boundary");
        const n = neighbors[i].size;
        if (!n) return w;
        const beta = n === 3 ? 3 / 16 : 3 / (8 * n);
        return mix([
          [w, 1 - n * beta],
          ...[...neighbors[i]].map(
            (j) => [weights[j], beta] as [Stencil, number],
          ),
        ]);
      });
      for (const e of edges.values()) {
        next.push(
          e.opposite.length === 1
            ? mix([
                [weights[e.a], 0.5],
                [weights[e.b], 0.5],
              ])
            : mix([
                [weights[e.a], 0.375],
                [weights[e.b], 0.375],
                [weights[e.opposite[0]], 0.125],
                [weights[e.opposite[1]], 0.125],
              ]),
        );
      }
      const refined: number[] = [];
      for (let f = 0; f < faces.length; f += 3) {
        const [a, b, c] = faces.slice(f, f + 3);
        const ab = edges.get(key(a, b))!.id,
          bc = edges.get(key(b, c))!.id,
          ca = edges.get(key(c, a))!.id;
        refined.push(a, ab, ca, b, bc, ab, c, ca, bc, ab, bc, ca);
      }
      weights = next;
      faces = refined;
    }
    this.stencils = weights;
    this.midSurface.setIndex(faces);
    this.midSurface.setAttribute(
      "position",
      new THREE.BufferAttribute(
        new Float32Array(weights.length * 3),
        3,
      ).setUsage(THREE.DynamicDrawUsage),
    );
    const uv = controlGeometry.getAttribute("uv");
    if (uv) {
      const values = new Float32Array(weights.length * 2);
      weights.forEach((row, i) => {
        for (const [j, w] of row) {
          values[2 * i] += w * uv.getX(j);
          values[2 * i + 1] += w * uv.getY(j);
        }
      });
      this.midSurface.setAttribute("uv", new THREE.BufferAttribute(values, 2));
    }
    if (normalOffset) {
      const n = weights.length;
      const shell = [...faces];
      const edges = new Map<string, {a: number; b: number; count: number}>();
      for (let f = 0; f < faces.length; f += 3) {
        const [a, b, c] = faces.slice(f, f + 3);
        shell.push(c + n, b + n, a + n);
        for (const [i, j] of [[a,b], [b,c], [c,a]]) {
          const key = [i,j].sort((x,y) => x-y).join(":");
          const e = edges.get(key);
          if (e) e.count++;
          else edges.set(key, {a:i, b:j, count:1});
        }
      }
      // Close only the fabric's cut edge, not the sock's cuff opening.
      for (const {a,b,count} of edges.values()) {
        if (count === 1) shell.push(b,a,a+n, b,a+n,b+n);
      }
      this.geometry.setIndex(shell);
      this.geometry.setAttribute("position", new THREE.BufferAttribute(
        new Float32Array(n * 6), 3).setUsage(THREE.DynamicDrawUsage));
      const midUV = this.midSurface.getAttribute("uv");
      if (midUV) {
        const values = new Float32Array(n * 4);
        values.set(midUV.array); values.set(midUV.array, n * 2);
        this.geometry.setAttribute("uv", new THREE.BufferAttribute(values, 2));
      }
    }
    this.update();
  }

  update(): void {
    const source = this.controlGeometry.getAttribute("position");
    const output = this.midSurface.getAttribute(
      "position",
    ) as THREE.BufferAttribute;
    this.stencils.forEach((row, i) => {
      let x = 0,
        y = 0,
        z = 0;
      for (const [j, w] of row) {
        x += w * source.getX(j);
        y += w * source.getY(j);
        z += w * source.getZ(j);
      }
      output.setXYZ(i, x, y, z);
    });
    output.needsUpdate = true;
    this.midSurface.computeVertexNormals();
    if (this.normalOffset) {
      const normals = this.midSurface.getAttribute("normal");
      const shell = this.geometry.getAttribute("position") as THREE.BufferAttribute;
      for (let i = 0; i < output.count; i++) {
        for (const side of [0, 1]) {
          const offset = this.normalOffset * (side === 0 ? 1 : -1);
          shell.setXYZ(i + side * output.count,
            output.getX(i) + normals.getX(i) * offset,
            output.getY(i) + normals.getY(i) * offset,
            output.getZ(i) + normals.getZ(i) * offset);
        }
      }
      shell.needsUpdate = true;
      this.geometry.computeVertexNormals();
    }
    this.geometry.computeBoundingBox();
    this.geometry.computeBoundingSphere();
  }
}
