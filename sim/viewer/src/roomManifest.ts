// Primitive-authored world geometry as the world server's roster frame
// describes it (statics.py RoomRegistry.manifest): a world with no mesh to
// download, drawn from these numbers alone. Sizes are MuJoCo half-extents in
// metres, poses are world Z-up, quaternions are MuJoCo order (w, x, y, z).
// Kept free of three.js so parsing and bounds run under plain `node --test`,
// the way trafficState.ts does for traffic.

export type RoomGeomType = "box" | "sphere" | "cylinder" | "capsule";

export interface RoomGeom {
  type: RoomGeomType;
  size: number[];
  pos: number[];
  quat: number[];
  rgba: number[];
  /** False for decor (floor seams, skirting): drawn, never collided with. */
  collide: boolean;
  /** The sidecar's name for the geom; "ceiling" marks a lid (see isCeiling). */
  name?: string;
}

export interface RoomInfo {
  name: string;
  title: string;
  geoms: RoomGeom[];
}

export interface Bounds {
  min: [number, number, number];
  max: [number, number, number];
}

/** How many size numbers each primitive carries (MuJoCo's convention). */
const SIZE_ARITY: Record<RoomGeomType, number> = { box: 3, sphere: 1, cylinder: 2, capsule: 2 };

function finiteNumbers(values: unknown, count: number): values is number[] {
  return (
    Array.isArray(values) &&
    values.length >= count &&
    values.slice(0, count).every((v) => typeof v === "number" && Number.isFinite(v))
  );
}

/** A geom the viewer can draw without throwing: a known type with enough
 * finite numbers for it. A malformed one is skipped, not fatal -- the world
 * server is authoritative and the view is only a view. */
export function isValidRoomGeom(geom: unknown): geom is RoomGeom {
  if (typeof geom !== "object" || geom === null) return false;
  const g = geom as Partial<RoomGeom>;
  if (typeof g.type !== "string" || !(g.type in SIZE_ARITY)) return false;
  const arity = SIZE_ARITY[g.type as RoomGeomType];
  if (!finiteNumbers(g.size, arity) || !finiteNumbers(g.pos, 3) || !finiteNumbers(g.quat, 4) || !finiteNumbers(g.rgba, 4)) {
    return false;
  }
  return g.size.slice(0, arity).every((v) => v > 0);
}

/** A room's lid. Real in the sim -- the robot's camera sees it instead of
 * black sky -- but never drawn here: the observer looks in from above, and
 * an opaque BoxGeometry at ceiling height hides the robot and every prop
 * under it. The apartment gets its cutaway from an inward-facing shell and
 * front-face culling, which a closed box cannot give, so the lid is left out
 * by name (statics.py names it "ceiling"). */
export function isCeiling(geom: RoomGeom): boolean {
  return geom.name === "ceiling";
}

/** Radius of the sphere enclosing the geom in its own frame, so a bound built
 * from it holds in any orientation without a per-geom rotation. */
export function geomRadius(geom: RoomGeom): number {
  const s = geom.size;
  switch (geom.type) {
    case "sphere":
      return s[0];
    case "cylinder":
      return Math.hypot(s[0], s[1]);
    case "capsule":
      return s[0] + s[1];
    default:
      return Math.hypot(s[0], s[1], s[2]);
  }
}

/** Axis-aligned extent of every valid geom in the rooms, or null when there is
 * nothing to bound. Conservative (bounding spheres), which is what framing and
 * the "top" camera want: the whole world in view, never a corner clipped. */
export function roomBounds(rooms: RoomInfo[]): Bounds | null {
  let bounds: Bounds | null = null;
  for (const room of rooms) {
    for (const geom of room.geoms) {
      if (!isValidRoomGeom(geom)) continue;
      const r = geomRadius(geom);
      const [x, y, z] = geom.pos;
      if (!bounds) {
        bounds = { min: [x - r, y - r, z - r], max: [x + r, y + r, z + r] };
        continue;
      }
      bounds.min = [Math.min(bounds.min[0], x - r), Math.min(bounds.min[1], y - r), Math.min(bounds.min[2], z - r)];
      bounds.max = [Math.max(bounds.max[0], x + r), Math.max(bounds.max[1], y + r), Math.max(bounds.max[2], z + r)];
    }
  }
  return bounds;
}
