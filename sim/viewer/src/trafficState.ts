export type TrafficAspect = "red" | "yellow" | "green";
export type TrafficManifest = { id: string; color: string }[];

export interface TrafficCarState {
  pose: [number, number, number];
  spawn_seq: number;
}

export interface TrafficState {
  world_epoch: number;
  signals: Record<string, TrafficAspect>;
  cars: Record<string, TrafficCarState>;
}

/** Hold discrete changes until the next sample: blending a respawn would
 * streak the recycled car through the town. Traffic shares the robot clock. */
export function interpolateTraffic(a: TrafficState | null, b: TrafficState | null, u: number): TrafficState | null {
  if (a === null || b === null || a.world_epoch !== b.world_epoch) return u < 1 ? a : b;
  const selected = u < 1 ? a : b;
  const cars = { ...selected.cars };
  for (const [id, from] of Object.entries(a.cars)) {
    const to = b.cars[id];
    if (!to || from.spawn_seq !== to.spawn_seq) continue;
    const dyaw = Math.atan2(Math.sin(to.pose[2] - from.pose[2]), Math.cos(to.pose[2] - from.pose[2]));
    cars[id] = {
      pose: [
        from.pose[0] + (to.pose[0] - from.pose[0]) * u,
        from.pose[1] + (to.pose[1] - from.pose[1]) * u,
        from.pose[2] + dyaw * u,
      ],
      spawn_seq: from.spawn_seq,
    };
  }
  return { ...selected, cars };
}
