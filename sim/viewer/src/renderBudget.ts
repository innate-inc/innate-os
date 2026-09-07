// Opt-in GPU fill budget, independent of the display's size and Retina scale.
// ~3 MP matches the tested 2147 x 1420 target; a DPR-only cap allowed 12 MP.
export const MAX_RENDER_PIXELS = 3_000_000;

export function stagePixelRatio(width: number, height: number, deviceRatio: number, reduced = false): number {
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) return 1;
  const nativeRatio = Number.isFinite(deviceRatio) && deviceRatio > 0 ? deviceRatio : 1;
  // Allow sub-1 scaling on very large windows. A floor of 1 would defeat the
  // budget there. Logical size/aspect and CSS overlays stay unchanged.
  const full = Math.min(nativeRatio, 2);
  // Always lower the buffer when requested, including small DPR-1 displays.
  return reduced ? Math.min(full * 0.75, Math.sqrt(MAX_RENDER_PIXELS / (width * height))) : full;
}
