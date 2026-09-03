// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Active simulator-environment routing shared by the scene loader and its
// dependency-free tests. Generic asset URLs carry the descriptor fingerprint,
// so one scene can never silently cross from one pack to another mid-load.

const DESCRIPTOR_URL = "/sim-environment/manifest.json";
const SCENE_URL = "/sim-environment/scene.glb";
const LAYOUT_URL = "/sim-environment/layout.json";
const ROOM_BASE_URL = "/sim-environment/rooms/";
const COLLISION_BASE_URL = "/sim-environment/collisions/";

export interface ManifestRoom {
  file: string;
  name: string;
  bytes: number;
  bbox: { min: number[]; max: number[] };
}

export type EnvironmentSource = {
  collisionBaseUrl: string;
  fingerprint: string;
} & (
  | { mode: "glb"; sceneUrl: string }
  | { mode: "split-glb"; manifestUrl: string; roomBaseUrl: string }
);

/** A first pose cancels the one-shot camera tween without changing the selected mode. */
export function shouldRestartTopCameraTween(mode: string, transitionActive: boolean): boolean {
  return mode === "top" && !transitionActive;
}

/** True only when catalog, visual assets, and the live physics world agree. */
export function environmentIdentitiesReady(
  active: { fingerprint?: string } | null,
  world: { fingerprint: string; connected: boolean } | null,
  sceneFingerprint: string | null,
): boolean {
  return Boolean(
    active?.fingerprint &&
      world?.connected &&
      world.fingerprint === active.fingerprint &&
      sceneFingerprint === active.fingerprint,
  );
}

/** Append the pack identity only to descriptor-backed generic asset routes. */
export function environmentAssetUrl(url: string, fingerprint: string): string {
  const separator = url.includes("?") ? "&" : "?";
  return `${url}${separator}fingerprint=${encodeURIComponent(fingerprint)}`;
}

function sourceFromDescriptor(value: unknown): EnvironmentSource | null {
  if (!value || typeof value !== "object") return null;
  const descriptor = value as { fingerprint?: unknown; viewer?: { type?: unknown } };
  const fingerprint = descriptor.fingerprint;
  if (typeof fingerprint !== "string" || !fingerprint) return null;
  if (descriptor.viewer?.type === "glb") {
    return {
      mode: "glb",
      sceneUrl: SCENE_URL,
      collisionBaseUrl: COLLISION_BASE_URL,
      fingerprint,
    };
  }
  if (descriptor.viewer?.type === "split-glb") {
    return {
      mode: "split-glb",
      manifestUrl: LAYOUT_URL,
      roomBaseUrl: ROOM_BASE_URL,
      collisionBaseUrl: COLLISION_BASE_URL,
      fingerprint,
    };
  }
  return null;
}

export async function resolveEnvironmentSource(
  fetchFn: typeof fetch = globalThis.fetch.bind(globalThis),
): Promise<EnvironmentSource> {
  const response = await fetchFn(DESCRIPTOR_URL, { cache: "no-store" });
  if (!response.ok) throw new Error(`simulator environment descriptor returned HTTP ${response.status}`);
  const source = sourceFromDescriptor(await response.json());
  if (!source) throw new Error("simulator environment descriptor is malformed");
  return source;
}

/** Runtime guard for the untrusted progressive-room manifest. */
export function isValidManifestRoom(room: unknown): room is ManifestRoom {
  const r = room as { file?: unknown; name?: unknown; bbox?: { min?: unknown; max?: unknown } } | null;
  const finite3 = (a: unknown): boolean =>
    Array.isArray(a) && a.length >= 3 && a.slice(0, 3).every((n) => typeof n === "number" && Number.isFinite(n));
  return (
    typeof r?.file === "string" &&
    typeof r?.name === "string" &&
    finite3(r?.bbox?.min) &&
    finite3(r?.bbox?.max)
  );
}
