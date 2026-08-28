/** Static mapping from a low-resolution simulated surface to a render mesh. */
export interface DeformableSkin {
  renderCount: number;
  controlCount: number;
  /** Three simulated-vertex indices per render vertex. */
  indices: Uint16Array;
  /** Barycentric weights corresponding to indices. */
  weights: Float32Array;
  /** Per-render-vertex offsets in the triangle's tangent1/tangent2/normal basis. */
  localOffsets: Float32Array;
}

export const ISK1_HEADER_BYTES = 16;
const ISK1_MAGIC_LE = 0x314b5349; // ASCII "ISK1" read little-endian.
const BASIS_EPSILON = 1e-12;

/** Decode a static ISK1 skin map.
 *
 * Layout (little-endian):
 *   char[4] magic = "ISK1"
 *   uint32 render_count
 *   uint32 control_count
 *   uint32 reserved = 0
 *   uint16 triangle_indices[render_count * 3]
 *   padding to a four-byte boundary
 *   float32 barycentric_weights[render_count * 3]
 *   float32 local_offsets[render_count * 3]
 */
export function decodeDeformableSkin(buffer: ArrayBuffer): DeformableSkin {
  if (buffer.byteLength < ISK1_HEADER_BYTES) {
    throw new Error(`ISK1 skin is ${buffer.byteLength} bytes; expected at least ${ISK1_HEADER_BYTES}`);
  }

  const view = new DataView(buffer);
  if (view.getUint32(0, true) !== ISK1_MAGIC_LE) throw new Error("invalid ISK1 magic");

  const renderCount = view.getUint32(4, true);
  const controlCount = view.getUint32(8, true);
  const reserved = view.getUint32(12, true);
  if (renderCount === 0) throw new Error("ISK1 render count must be positive");
  if (controlCount === 0) throw new Error("ISK1 control count must be positive");
  if (reserved !== 0) throw new Error(`unsupported ISK1 flags/reserved value ${reserved}`);

  const tupleCount = renderCount * 3;
  const indicesOffset = ISK1_HEADER_BYTES;
  const indicesBytes = tupleCount * Uint16Array.BYTES_PER_ELEMENT;
  const weightsOffset = (indicesOffset + indicesBytes + 3) & ~3;
  const valuesBytes = tupleCount * Float32Array.BYTES_PER_ELEMENT;
  const offsetsOffset = weightsOffset + valuesBytes;
  const expectedBytes = offsetsOffset + valuesBytes;
  if (buffer.byteLength !== expectedBytes) {
    throw new Error(`ISK1 skin is ${buffer.byteLength} bytes; expected exactly ${expectedBytes}`);
  }

  const indices = new Uint16Array(buffer, indicesOffset, tupleCount);
  const weights = new Float32Array(buffer, weightsOffset, tupleCount);
  const localOffsets = new Float32Array(buffer, offsetsOffset, tupleCount);
  for (let i = 0; i < tupleCount; i += 1) {
    if (indices[i] >= controlCount) {
      throw new Error(`ISK1 control index ${indices[i]} at element ${i} exceeds control count ${controlCount}`);
    }
    if (!Number.isFinite(weights[i])) throw new Error(`ISK1 weight ${i} is not finite`);
    if (!Number.isFinite(localOffsets[i])) throw new Error(`ISK1 local offset ${i} is not finite`);
  }

  return { renderCount, controlCount, indices, weights, localOffsets };
}

/** CPU-skin a render mesh from its simulated control surface.
 *
 * For each render vertex, the triangle is ordered A/B/C. tangent1 is the
 * normalized A->B edge, normal is normalize((B-A) x (C-A)), and tangent2 is
 * normal x tangent1. This convention must match the ISK1 asset generator.
 */
export function skinDeformablePositions(
  skin: DeformableSkin,
  controls: Float32Array,
  output: Float32Array = new Float32Array(skin.renderCount * 3),
): Float32Array {
  const expectedControls = skin.controlCount * 3;
  const expectedOutput = skin.renderCount * 3;
  if (controls.length !== expectedControls) {
    throw new Error(`control buffer has ${controls.length} values; expected ${expectedControls}`);
  }
  if (output.length !== expectedOutput) {
    throw new Error(`render buffer has ${output.length} values; expected ${expectedOutput}`);
  }

  const { indices, weights, localOffsets } = skin;
  for (let i = 0; i < skin.renderCount; i += 1) {
    const tuple = i * 3;
    const ai = indices[tuple] * 3;
    const bi = indices[tuple + 1] * 3;
    const ci = indices[tuple + 2] * 3;
    const ax = controls[ai];
    const ay = controls[ai + 1];
    const az = controls[ai + 2];
    const bx = controls[bi];
    const by = controls[bi + 1];
    const bz = controls[bi + 2];
    const cx = controls[ci];
    const cy = controls[ci + 1];
    const cz = controls[ci + 2];

    const e1x = bx - ax;
    const e1y = by - ay;
    const e1z = bz - az;
    const e2x = cx - ax;
    const e2y = cy - ay;
    const e2z = cz - az;

    let t1x = e1x;
    let t1y = e1y;
    let t1z = e1z;
    let t1Length = Math.hypot(t1x, t1y, t1z);
    if (t1Length < BASIS_EPSILON) {
      t1x = e2x;
      t1y = e2y;
      t1z = e2z;
      t1Length = Math.hypot(t1x, t1y, t1z);
    }
    if (t1Length < BASIS_EPSILON) {
      t1x = 1;
      t1y = 0;
      t1z = 0;
    } else {
      t1x /= t1Length;
      t1y /= t1Length;
      t1z /= t1Length;
    }

    let nx = e1y * e2z - e1z * e2y;
    let ny = e1z * e2x - e1x * e2z;
    let nz = e1x * e2y - e1y * e2x;
    let normalLength = Math.hypot(nx, ny, nz);
    if (normalLength < BASIS_EPSILON) {
      // Pick the least-aligned cardinal axis and cross it with tangent1.
      // This is only a finite fallback for a collapsed runtime triangle; the
      // generator and normal operation both use the triangle cross product.
      if (Math.abs(t1x) <= Math.abs(t1y) && Math.abs(t1x) <= Math.abs(t1z)) {
        nx = 0;
        ny = t1z;
        nz = -t1y;
      } else if (Math.abs(t1y) <= Math.abs(t1z)) {
        nx = -t1z;
        ny = 0;
        nz = t1x;
      } else {
        nx = t1y;
        ny = -t1x;
        nz = 0;
      }
      normalLength = Math.hypot(nx, ny, nz);
    }
    nx /= normalLength;
    ny /= normalLength;
    nz /= normalLength;

    // tangent2 = normal x tangent1.
    let t2x = ny * t1z - nz * t1y;
    let t2y = nz * t1x - nx * t1z;
    let t2z = nx * t1y - ny * t1x;
    const t2Length = Math.hypot(t2x, t2y, t2z) || 1;
    t2x /= t2Length;
    t2y /= t2Length;
    t2z /= t2Length;

    const wa = weights[tuple];
    const wb = weights[tuple + 1];
    const wc = weights[tuple + 2];
    const ox = localOffsets[tuple];
    const oy = localOffsets[tuple + 1];
    const oz = localOffsets[tuple + 2];
    output[tuple] = wa * ax + wb * bx + wc * cx + ox * t1x + oy * t2x + oz * nx;
    output[tuple + 1] = wa * ay + wb * by + wc * cy + ox * t1y + oy * t2y + oz * ny;
    output[tuple + 2] = wa * az + wb * bz + wc * cz + ox * t1z + oy * t2z + oz * nz;
  }
  return output;
}
