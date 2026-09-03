export type TrafficAspect = "red" | "yellow" | "green";

export interface TrafficPart {
  shape: "box" | "cylinder";
  position: [number, number, number];
  material: "body" | "glass" | "rubber" | "headlight" | "taillight";
  size?: [number, number, number];
  radius?: number;
  length?: number;
  rotation?: [number, number, number];
}

export interface TrafficCollider {
  shape: "box";
  position: [number, number, number];
  size: [number, number, number];
}

export interface TrafficCarModel {
  forward_axis: "+x";
  up_axis: "+z";
  length: number;
  width: number;
  height: number;
  parts: TrafficPart[];
  colliders: TrafficCollider[];
  materials: Record<"glass" | "rubber" | "headlight" | "taillight", string>;
}

export interface TrafficManifest {
  schema_version: 1;
  car_model: TrafficCarModel;
  cars: { id: string; color: string }[];
  signal_materials: Record<string, Record<TrafficAspect, string>>;
  signal_colors: Record<TrafficAspect, string>;
}

export interface TrafficCarState {
  pose: [number, number, number];
  speed: number;
  spawn_seq: number;
}

export interface TrafficState {
  world_epoch: number;
  phase: string;
  signals: Record<string, TrafficAspect>;
  cars: Record<string, TrafficCarState>;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function finiteTuple3(value: unknown): value is [number, number, number] {
  return Array.isArray(value) && value.length === 3 && value.every((item) => typeof item === "number" && Number.isFinite(item));
}

function positive(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value > 0;
}

const MATERIAL_ROLES = new Set<TrafficPart["material"]>(["body", "glass", "rubber", "headlight", "taillight"]);

export function parseTrafficManifest(value: unknown): TrafficManifest | null {
  if (!isRecord(value) || value.schema_version !== 1 || !isRecord(value.car_model)) return null;
  const model = value.car_model;
  if (
    model.forward_axis !== "+x" ||
    model.up_axis !== "+z" ||
    !positive(model.length) ||
    !positive(model.width) ||
    !positive(model.height) ||
    !Array.isArray(model.parts) ||
    model.parts.length === 0 ||
    !Array.isArray(model.colliders) ||
    model.colliders.length === 0 ||
    !isRecord(model.materials) ||
    !Array.isArray(value.cars) ||
    !isRecord(value.signal_materials) ||
    !isRecord(value.signal_colors)
  ) {
    return null;
  }
  for (const part of model.parts) {
    if (!isRecord(part) || (part.shape !== "box" && part.shape !== "cylinder") || !finiteTuple3(part.position)) return null;
    if (typeof part.material !== "string" || !MATERIAL_ROLES.has(part.material as TrafficPart["material"])) return null;
    if (part.rotation !== undefined && !finiteTuple3(part.rotation)) return null;
    if (part.shape === "box" && (!finiteTuple3(part.size) || !part.size.every(positive))) return null;
    if (part.shape === "cylinder" && (!positive(part.radius) || !positive(part.length) || !finiteTuple3(part.rotation))) return null;
  }
  for (const collider of model.colliders) {
    if (
      !isRecord(collider) ||
      collider.shape !== "box" ||
      !finiteTuple3(collider.position) ||
      !finiteTuple3(collider.size) ||
      !collider.size.every(positive)
    ) {
      return null;
    }
  }
  for (const role of ["glass", "rubber", "headlight", "taillight"] as const) {
    if (typeof model.materials[role] !== "string" || !model.materials[role]) return null;
  }
  const ids = new Set<string>();
  for (const car of value.cars) {
    if (!isRecord(car) || typeof car.id !== "string" || !car.id || typeof car.color !== "string" || !car.color || ids.has(car.id)) {
      return null;
    }
    ids.add(car.id);
  }
  if (Object.keys(value.signal_materials).length === 0) return null;
  for (const aspects of Object.values(value.signal_materials)) {
    if (!isRecord(aspects)) return null;
    for (const aspect of ["red", "yellow", "green"] as const) {
      if (typeof aspects[aspect] !== "string" || !aspects[aspect]) return null;
    }
  }
  for (const aspect of ["red", "yellow", "green"] as const) {
    if (typeof value.signal_colors[aspect] !== "string") return null;
  }
  return value as unknown as TrafficManifest;
}

export function parseTrafficState(value: unknown): TrafficState | null {
  if (!isRecord(value)) return null;
  if (
    !Number.isInteger(value.world_epoch) ||
    (value.world_epoch as number) < 0 ||
    typeof value.phase !== "string" ||
    !isRecord(value.signals) ||
    !isRecord(value.cars)
  ) {
    return null;
  }
  const signals: Record<string, TrafficAspect> = {};
  for (const [group, aspect] of Object.entries(value.signals)) {
    if (aspect !== "red" && aspect !== "yellow" && aspect !== "green") return null;
    signals[group] = aspect;
  }
  const cars: Record<string, TrafficCarState> = {};
  for (const [id, raw] of Object.entries(value.cars)) {
    if (!isRecord(raw) || !finiteTuple3(raw.pose) || typeof raw.speed !== "number" || !Number.isFinite(raw.speed)) return null;
    if (!Number.isInteger(raw.spawn_seq) || (raw.spawn_seq as number) < 0 || raw.speed < 0) return null;
    cars[id] = { pose: raw.pose, speed: raw.speed, spawn_seq: raw.spawn_seq as number };
  }
  return {
    world_epoch: value.world_epoch as number,
    phase: value.phase,
    signals,
    cars,
  };
}

/** Interpolate traffic on the robot's playback timeline. Signal changes and
 * respawns are discrete: holding a red to the next sample is safe, while
 * blending a recycled car would streak it through the entire town. */
export function interpolateTraffic(a: TrafficState | null, b: TrafficState | null, u: number): TrafficState | null {
  if (a === null || b === null || a.world_epoch !== b.world_epoch) return u < 1 ? a : b;
  const cars: Record<string, TrafficCarState> = {};
  const ids = new Set([...Object.keys(a.cars), ...Object.keys(b.cars)]);
  for (const id of ids) {
    const from = a.cars[id];
    const to = b.cars[id];
    if (!from || !to || from.spawn_seq !== to.spawn_seq) {
      const selected = u < 1 ? from : to;
      if (selected) cars[id] = { ...selected, pose: [...selected.pose] };
      continue;
    }
    const dyaw = Math.atan2(Math.sin(to.pose[2] - from.pose[2]), Math.cos(to.pose[2] - from.pose[2]));
    cars[id] = {
      pose: [
        from.pose[0] + (to.pose[0] - from.pose[0]) * u,
        from.pose[1] + (to.pose[1] - from.pose[1]) * u,
        from.pose[2] + dyaw * u,
      ],
      speed: from.speed + (to.speed - from.speed) * u,
      spawn_seq: from.spawn_seq,
    };
  }
  const discrete = u < 1 ? a : b;
  return {
    world_epoch: a.world_epoch,
    phase: discrete.phase,
    signals: { ...discrete.signals },
    cars,
  };
}
