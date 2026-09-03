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

export interface TrafficCarModel {
  length: number;
  width: number;
  parts: TrafficPart[];
  colliders: { shape: "box"; position: [number, number, number]; size: [number, number, number] }[];
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

function tuple3(value: unknown, positive = false): value is [number, number, number] {
  return (
    Array.isArray(value) &&
    value.length === 3 &&
    value.every((item) => typeof item === "number" && Number.isFinite(item) && (!positive || item > 0))
  );
}

const MATERIAL_ROLES = new Set<TrafficPart["material"]>(["body", "glass", "rubber", "headlight", "taillight"]);
const ASPECTS = ["red", "yellow", "green"] as const;
const nonEmptyString = (value: unknown): value is string => typeof value === "string" && value.length > 0;
const positiveNumber = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value) && value > 0;

export function parseTrafficManifest(value: unknown): TrafficManifest | null {
  if (!isRecord(value) || value.schema_version !== 1 || !isRecord(value.car_model)) return null;
  const model = value.car_model;
  if (
    !positiveNumber(model.length) ||
    !positiveNumber(model.width) ||
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
    if (!isRecord(part) || !tuple3(part.position)) return null;
    if (typeof part.material !== "string" || !MATERIAL_ROLES.has(part.material as TrafficPart["material"])) return null;
    if (part.rotation !== undefined && !tuple3(part.rotation)) return null;
    if (part.shape === "box" ? !tuple3(part.size, true) : part.shape !== "cylinder" || !positiveNumber(part.radius) || !positiveNumber(part.length) || !tuple3(part.rotation)) return null;
  }
  for (const collider of model.colliders) {
    if (!isRecord(collider) || collider.shape !== "box" || !tuple3(collider.position) || !tuple3(collider.size, true)) return null;
  }
  for (const role of ["glass", "rubber", "headlight", "taillight"] as const) {
    if (!nonEmptyString(model.materials[role])) return null;
  }
  const ids = new Set<string>();
  for (const car of value.cars) {
    if (!isRecord(car) || !nonEmptyString(car.id) || !nonEmptyString(car.color) || ids.has(car.id)) {
      return null;
    }
    ids.add(car.id);
  }
  if (Object.keys(value.signal_materials).length === 0) return null;
  for (const aspects of Object.values(value.signal_materials)) {
    if (!isRecord(aspects) || ASPECTS.some((aspect) => !nonEmptyString(aspects[aspect]))) return null;
  }
  const signalColors = value.signal_colors as Record<string, unknown>;
  if (ASPECTS.some((aspect) => !nonEmptyString(signalColors[aspect]))) return null;
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
  if (Object.values(value.signals).some((aspect) => aspect !== "red" && aspect !== "yellow" && aspect !== "green")) {
    return null;
  }
  for (const raw of Object.values(value.cars)) {
    if (!isRecord(raw) || !tuple3(raw.pose) || typeof raw.speed !== "number" || !Number.isFinite(raw.speed)) return null;
    if (!Number.isInteger(raw.spawn_seq) || (raw.spawn_seq as number) < 0 || raw.speed < 0) return null;
  }
  return value as unknown as TrafficState;
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
