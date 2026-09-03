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

const LEGACY_APARTMENT_SOURCE: EnvironmentSource = {
  mode: "split-glb",
  manifestUrl: "/models/apartment/manifest.json",
  roomBaseUrl: "/models/apartment/",
  sceneUrl: "/models/appartement.glb",
  collisionBaseUrl: "/physics/apartment_collisions_v2/",
};

export interface ManifestRoom {
  file: string;
  name: string;
  bytes: number;
  bbox: { min: number[]; max: number[] };
}

export interface EnvironmentSource {
  mode: "split-glb" | "glb";
  manifestUrl?: string;
  roomBaseUrl?: string;
  sceneUrl?: string;
  collisionBaseUrl: string;
  /** Present only for descriptor-backed generic routes, never legacy paths. */
  fingerprint?: string;
}

/** Descriptor-backed routes promise one exact pack, so missing geometry must
 * fail the load. Only the fingerprint-less old-proxy apartment keeps the
 * historical best-effort visual behavior. */
export function requiresCompleteEnvironment(fingerprint?: string, strict = false): boolean {
  return strict || Boolean(fingerprint);
}

/** Append the pack identity only to descriptor-backed generic asset routes. */
export function environmentAssetUrl(url: string, fingerprint?: string): string {
  if (!fingerprint) return url;
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

const RETRY_DELAY_MS = 100;
const RETRY_ATTEMPTS = 3;

/**
 * Resolve the active pack, retrying descriptor failures before considering the
 * legacy apartment routes. A persistent 404 means an older proxy; network,
 * server, or malformed-response failures remain errors instead of pinning the
 * scene to geometry that can disagree with the running physics world.
 */
export async function resolveEnvironmentSource(
  fetchFn: typeof fetch = globalThis.fetch.bind(globalThis),
  delayFn: (milliseconds: number) => Promise<void> = (milliseconds) =>
    new Promise((resolve) => globalThis.setTimeout(resolve, milliseconds)),
  attempts = RETRY_ATTEMPTS,
): Promise<EnvironmentSource> {
  let lastError: unknown = new Error("simulator environment descriptor unavailable");
  for (let attempt = 0; attempt < Math.max(1, attempts); attempt += 1) {
    try {
      const response = await fetchFn(DESCRIPTOR_URL, { cache: "no-store" });
      if (response.status === 404) {
        if (attempt + 1 >= Math.max(1, attempts)) return { ...LEGACY_APARTMENT_SOURCE };
        lastError = new Error("simulator environment descriptor not found");
      } else if (!response.ok) {
        lastError = new Error(`simulator environment descriptor returned HTTP ${response.status}`);
      } else {
        const source = sourceFromDescriptor(await response.json());
        if (source) return source;
        lastError = new Error("simulator environment descriptor is malformed");
      }
    } catch (error) {
      lastError = error;
    }
    if (attempt + 1 < Math.max(1, attempts)) await delayFn(RETRY_DELAY_MS);
  }
  throw lastError;
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
