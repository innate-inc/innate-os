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
