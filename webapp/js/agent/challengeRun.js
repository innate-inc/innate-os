// @ts-check
// Wait for the judge's new attempt before letting the agent see the prompt.
// Retrying the same challenge must not accept the previous attempt's state.

/** @param {any} session @param {string} id @param {AbortSignal} signal
 * @param {number} [timeoutMs] @returns {Promise<string>} */
export function startChallengeAndWait(session, id, signal, timeoutMs = 15_000) {
  return new Promise((resolve, reject) => {
    let sent = false;
    let previous = "";
    let unsubscribe = () => {};
    const finish = (/** @type {Error | null} */ error, attempt = "") => {
      clearTimeout(timer);
      unsubscribe();
      signal.removeEventListener("abort", cancel);
      if (error) reject(error);
      else resolve(attempt);
    };
    const cancel = () => finish(new Error("Challenge start cancelled."));
    const timer = setTimeout(() => finish(new Error("The scene did not become ready. Please try again.")), timeoutMs);
    unsubscribe = session.onChallenge((/** @type {any} */ block) => {
      const active = block.active;
      if (!sent) { previous = active?.attempt_id ?? ""; return; }
      if (active?.id !== id || !active.attempt_id || active.attempt_id === previous) return;
      if (active.state !== "running") {
        finish(new Error(active.reason || "This challenge could not start."));
      } else finish(null, active.attempt_id);
    });
    signal.addEventListener("abort", cancel, { once: true });
    if (signal.aborted) { cancel(); return; }
    sent = true;
    try {
      if (session.startChallenge(id) === false) finish(new Error("The simulator is disconnected. Reconnect and try again."));
    } catch (error) {
      finish(error instanceof Error ? error : new Error("Could not start the challenge."));
    }
  });
}
