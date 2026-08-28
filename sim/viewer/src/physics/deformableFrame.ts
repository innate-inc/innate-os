/** One world-space control-mesh update from the world server. */
export interface DeformableFrame {
  id: number;
  t: number;
  vertexCount: number;
  /** Flat xyz coordinates, in metres and in the viewer's world frame. */
  positions: Float32Array;
}

export const IDF1_HEADER_BYTES = 24;
const IDF1_MAGIC_LE = 0x31464449; // ASCII "IDF1" read little-endian.

/** Decode the compact deformable-frame wire format streamed over WebSocket.
 *
 * Layout (little-endian):
 *   char[4] magic = "IDF1"
 *   uint32 id
 *   float64 simulation_time_s
 *   uint32 vertex_count
 *   uint32 reserved = 0
 *   float32 positions[vertex_count * 3]
 */
export function decodeDeformableFrame(buffer: ArrayBuffer): DeformableFrame {
  if (buffer.byteLength < IDF1_HEADER_BYTES) {
    throw new Error(`IDF1 frame is ${buffer.byteLength} bytes; expected at least ${IDF1_HEADER_BYTES}`);
  }

  const view = new DataView(buffer);
  if (view.getUint32(0, true) !== IDF1_MAGIC_LE) throw new Error("invalid IDF1 magic");

  const id = view.getUint32(4, true);
  const t = view.getFloat64(8, true);
  const vertexCount = view.getUint32(16, true);
  const reserved = view.getUint32(20, true);
  if (!Number.isFinite(t)) throw new Error("IDF1 simulation time is not finite");
  if (vertexCount === 0) throw new Error("IDF1 vertex count must be positive");
  if (reserved !== 0) throw new Error(`unsupported IDF1 flags/reserved value ${reserved}`);

  const positionCount = vertexCount * 3;
  const expectedBytes = IDF1_HEADER_BYTES + positionCount * Float32Array.BYTES_PER_ELEMENT;
  if (buffer.byteLength !== expectedBytes) {
    throw new Error(`IDF1 frame is ${buffer.byteLength} bytes; expected exactly ${expectedBytes}`);
  }

  const positions = new Float32Array(buffer, IDF1_HEADER_BYTES, positionCount);
  for (let i = 0; i < positions.length; i += 1) {
    if (!Number.isFinite(positions[i])) throw new Error(`IDF1 position ${i} is not finite`);
  }
  return { id, t, vertexCount, positions };
}
