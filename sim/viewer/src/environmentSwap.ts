// Latest-only coordinator for expensive environment scene builds. A scene
// that finishes after a newer request is disposed without ever becoming
// visible, so asynchronous downloads cannot roll the viewer back.

/**
 * Claim one transition/fingerprint request before its synchronous readiness
 * callback can echo the same switch state back into the stage.
 */
export function claimEnvironmentSwapKey(current: string, requested: string): string | null {
  return current === requested ? null : requested;
}

export interface EnvironmentSwapClaim {
  key: string;
}

/**
 * Announce a failed scene build while its request remains claimed, then release
 * that claim only when the backoff expires. Event dispatch is synchronous in
 * the webapp, so releasing before announce() would let its switch-state echo
 * start the same expensive build immediately and bypass the delay.
 */
export function deferEnvironmentSwapRetry<Timer>(
  claim: EnvironmentSwapClaim,
  requestedKey: string,
  announce: () => void,
  schedule: (callback: () => void, delayMs: number) => Timer,
  shouldRetry: () => boolean,
  retry: () => void,
): Timer | null {
  if (claim.key !== requestedKey) return null;
  announce();
  return schedule(() => {
    if (claim.key !== requestedKey) return;
    claim.key = "";
    if (shouldRetry()) retry();
  }, 1000);
}

interface EnvironmentSwapScene<CameraMode, CameraView> {
  setRenderSize(width: number, height: number, pixelRatio: number): void;
  setSafeInsets(insets: { right?: number }): void;
  setCameraMode(mode: CameraMode): void;
  setView(view: CameraView): void;
  render(): void;
}

/**
 * Prepare a replacement scene completely off-DOM before revealing it. The
 * first world tick may spawn the robot and reset its camera, so responsive
 * presentation state is deliberately restored only after that tick.
 */
export function prepareEnvironmentSwapScene<
  CameraMode,
  CameraView,
  Scene extends EnvironmentSwapScene<CameraMode, CameraView>,
>(
  scene: Scene,
  tick: (scene: Scene) => void,
  presentation: {
    width: number;
    height: number;
    pixelRatio: number;
    safeInsetRight: number;
    cameraMode: CameraMode;
    view: CameraView;
  },
): void {
  scene.setRenderSize(presentation.width, presentation.height, presentation.pixelRatio);
  tick(scene);
  scene.setSafeInsets({ right: presentation.safeInsetRight });
  scene.setCameraMode(presentation.cameraMode);
  scene.setView(presentation.view);
  scene.render();
}

export class EnvironmentSwapCoordinator<T> {
  #generation = 0;
  #disposed = false;

  async replace(
    build: (generation: number) => Promise<T>,
    activate: (candidate: T, generation: number) => void,
    discard: (candidate: T) => void,
  ): Promise<boolean> {
    const generation = ++this.#generation;
    const candidate = await build(generation);
    if (this.#disposed || generation !== this.#generation) {
      discard(candidate);
      return false;
    }
    try {
      activate(candidate, generation);
    } catch (error) {
      discard(candidate);
      throw error;
    }
    return true;
  }

  invalidate(): void {
    this.#generation += 1;
  }

  isCurrent(generation: number): boolean {
    return !this.#disposed && generation === this.#generation;
  }

  dispose(): void {
    this.#disposed = true;
    this.invalidate();
  }
}
