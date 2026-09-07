// @ts-check
// Challenge panel (sim only) — a stage launcher beside Scene Setup. Renders the
// world server's challenge judge state relayed through the sim session
// (session.onChallenge, see challenges.py): a roster of challenges to start,
// and while one runs, its goal checklist, timer, and pass/fail banner. All
// judging happens server-side against ground truth; this panel is a thin
// renderer plus two commands (start/abort).

import { maybeShowChallengeIntro, showChallengeIntro } from "./challengeIntro.js";

// This panel and the sim's scene setup expand over the same corner of the stage,
// so at most one may be open. Scene setup lives in the separately-built sim
// viewer bundle (sim/viewer/src/simStage.ts), so the handshake is a document
// event rather than a shared module: the event name and the detail.panel values
// are a contract with it.
const PANEL_OPEN_EVENT = "innate:panel-open";
const PANEL_ID = "agent-challenges";

/**
 * @param {HTMLElement} root
 * @param {any} session sim session exposing onChallenge/startChallenge/abortChallenge
 * @param {any} [onboarding] first-run participation, using the same scene challenge
 * @returns {{ destroy: () => void, dismiss: () => void }}
 */
export function createChallengePanel(root, session, onboarding) {
  let firstRun = /** @type {any} */ (null);
  let latest = /** @type {any} */ ({list:[], active:null});
  let environmentName = "";
  const guided = () => !!(firstRun?.active && firstRun.mission);
  const dock = document.createElement("div");
  dock.className = "agent-challenge-dock";
  dock.hidden = true;

  const panel = document.createElement("section");
  panel.id = "agent-challenge-panel";
  panel.className = "challenge-panel";
  panel.setAttribute("aria-label", "Challenges");

  const head = document.createElement("div");
  head.className = "challenge-head";
  const title = document.createElement("span");
  title.className = "microlabel";
  title.textContent = "Challenges";
  head.appendChild(title);

  const launcher = document.createElement("button");
  launcher.type = "button";
  launcher.className = "agent-challenge-toggle";
  launcher.setAttribute("aria-controls", panel.id);
  launcher.innerHTML =
    '<span class="agent-challenge-toggle-icon agent-challenge-toggle-flag" aria-hidden="true"></span>' +
    '<svg class="agent-challenge-toggle-icon agent-challenge-toggle-close" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="m7 7 10 10M17 7 7 17"/></svg>';
  let open = false;
  let challengeRunning = false;
  const setOpen = (/** @type {boolean} */ next) => {
    if (!next && guided()) return;
    open = next;
    dock.classList.toggle("open", next);
    launcher.setAttribute("aria-expanded", String(next));
    launcher.setAttribute("aria-label", next ? "Close challenges" : "Open challenges");
    if (!next) return;
    document.dispatchEvent(new CustomEvent(PANEL_OPEN_EVENT, { detail: { panel: PANEL_ID } }));
    if (revealed || firstRun?.active) return;
    revealed = true;
    intro = maybeShowChallengeIntro();
  };
  // Deliberately opening the other panel closes this one even mid-run — unlike
  // dismiss(), which protects a live goal list from a stray click on the scene.
  const onPanelOpen = (/** @type {Event} */ event) => {
    const opened = /** @type {CustomEvent<{ panel?: string }>} */ (event).detail?.panel;
    if (opened !== PANEL_ID && open) setOpen(false);
  };
  document.addEventListener(PANEL_OPEN_EVENT, onPanelOpen);
  // Touch only, and never over a live goal list — the rule dismiss() follows.
  const coarsePointer = window.matchMedia("(hover: none)");
  const onOutsidePointer = (/** @type {PointerEvent} */ event) => {
    if (!open || !coarsePointer.matches || challengeRunning) return;
    if (event.target instanceof Node && dock.contains(event.target)) return;
    setOpen(false);
  };
  document.addEventListener("pointerdown", onOutsidePointer, {capture:true});
  launcher.addEventListener("click", () => setOpen(!open));
  // Subtle standing hint back to the docs — reopens the first-run intro
  // (challengeIntro.js) with the tutorial link and preview.
  const tutorial = document.createElement("button");
  tutorial.type = "button";
  tutorial.className = "challenge-tutorial-link";
  // Circled "?" so the hint reads as a clickable help control, not a label
  // like the CHALLENGES microlabel next to it.
  const q = document.createElement("span");
  q.className = "challenge-tutorial-q";
  q.textContent = "?";
  q.setAttribute("aria-hidden", "true");
  const tutorialLabel = document.createElement("span");
  tutorialLabel.textContent = "Tutorial";
  tutorial.append(q, tutorialLabel);
  tutorial.title = "How challenges work, and the tutorial that builds your first skill";
  tutorial.addEventListener("click", () => {
    intro?.close();
    intro = showChallengeIntro();
  });
  head.appendChild(tutorial);
  /** Open intro dialog, if any — closed on page teardown, not leaked. */
  /** @type {{ close: () => void } | null} */
  let intro = null;
  let revealed = false;

  const body = document.createElement("div");
  panel.append(head, body);
  dock.append(panel, launcher);
  root.appendChild(dock);
  setOpen(false);

  /** Last rendered structure (block minus the ticking clock) — the timer text
   * updates in place so the DOM isn't rebuilt 10x a second. */
  let renderedKey = "";
  /** @type {HTMLElement | null} */
  let timerEl = null;

  function render() {
    const block = latest;
    dock.hidden = false;
    // A world switch can deliver the old scene's active challenge while the
    // selected mission is loading. Never display or control that foreign run.
    const active = guided() && block.active?.attempt_id !== firstRun.attemptId ? null : block.active;
    challengeRunning = active?.state === "running";
    dock.classList.toggle("active", challengeRunning);
    dock.classList.toggle("first-mission-challenge", guided());
    launcher.hidden = guided();
    tutorial.hidden = !!firstRun?.active;
    const scope = guided() ? firstRun.mission.setting : environmentName;
    title.textContent = scope ? `Challenges · ${scope}` : "Challenges";
    if (guided() && !open) setOpen(true);
    if (timerEl && active) timerEl.textContent = timerText(active);
    const key = JSON.stringify({ ...block, active: active && { ...active, elapsed_s: null }, firstRun, environmentName });
    if (key === renderedKey) return;
    renderedKey = key;
    body.replaceChildren(active ? renderActive(active, block.list) : guided() ? renderPreparing() : renderList(block.list));
  }
  const unsub = session.onChallenge((/** @type {any} */ block) => {
    latest = block;
    render();
  });
  const unsubEnvironment = session.onEnvironment?.((/** @type {any} */ value) => {
    environmentName = value.environment?.display_name || value.environment?.id || "";
    render();
  });
  const unsubFirstRun = onboarding?.subscribe((/** @type {any} */ value) => { firstRun = value; render(); });

  function renderPreparing() {
    timerEl = null;
    const wrap = document.createElement("div");
    wrap.className = "challenge-active";
    const name = document.createElement("strong");
    name.className = "challenge-item-title";
    name.textContent = firstRun.mission.title;
    const brief = document.createElement("div");
    brief.className = "challenge-brief";
    brief.textContent = firstRun.mission.brief;
    wrap.append(name, brief, guidedActions());
    return wrap;
  }

  function guidedActions() {
    const wrap = document.createElement("div");
    const status = document.createElement("p");
    status.className = "challenge-brief";
    status.setAttribute("role", "status");
    status.textContent = firstRun.status || "Guide MARS through chat. If something fails, ask it to try again.";
    const actions = document.createElement("div");
    actions.className = "challenge-actions";
    actions.append(actionButton("Skip mission", () => void onboarding.skip(), "Leave this mission and explore the interface"));
    wrap.append(status, actions);
    return wrap;
  }

  /** @param {any[]} list */
  function renderList(list) {
    timerEl = null;
    const wrap = document.createElement("div");
    wrap.className = "challenge-list";
    if (!list.length) {
      const empty = document.createElement("div");
      empty.className = "challenge-empty";
      empty.textContent = "No challenges in this environment yet.";
      wrap.append(empty);
      return wrap;
    }
    for (const c of list) {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "challenge-item";
      const dot = document.createElement("span");
      dot.className = `challenge-dot${c.passed ? " passed" : ""}`;
      dot.title = c.passed ? "Passed at least once" : "Not passed yet";
      const text = document.createElement("div");
      text.className = "challenge-item-text";
      const name = document.createElement("div");
      name.className = "challenge-item-title";
      name.textContent = c.title;
      const meta = document.createElement("div");
      meta.className = "challenge-item-meta";
      meta.textContent = c.passed
        ? `passed${c.best_time_s != null ? ` · best ${fmtClock(c.best_time_s)}` : ""}`
        : c.attempts
          ? `${c.attempts} ${c.attempts === 1 ? "attempt" : "attempts"}`
          : "not attempted";
      text.append(name, meta);
      const play = document.createElement("span");
      play.className = "challenge-play";
      play.innerHTML =
        '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="9,6 15,12 9,18"/></svg>';
      item.append(dot, text, play);
      item.title = c.brief;
      item.addEventListener("click", () => session.startChallenge(c.id));
      wrap.append(item);
    }
    return wrap;
  }

  /**
   * @param {any} active
   * @param {any[]} list
   */
  function renderActive(active, list) {
    const info = list.find((c) => c.id === active.id);
    const wrap = document.createElement("div");
    wrap.className = "challenge-active";

    const titleRow = document.createElement("div");
    titleRow.className = "challenge-title-row";
    const name = document.createElement("span");
    name.className = "challenge-item-title";
    name.textContent = info ? info.title : active.id;
    timerEl = document.createElement("span");
    timerEl.className = `challenge-timer${active.state !== "running" ? " final" : ""}`;
    timerEl.textContent = timerText(active);
    timerEl.title = active.state !== "running" ? "Final run time"
      : active.time_limit_s != null ? "Time remaining before the challenge fails" : "Time spent on this challenge";
    titleRow.append(name, timerEl);
    wrap.append(titleRow);

    if (info && active.state === "running") {
      const brief = document.createElement("div");
      brief.className = "challenge-brief";
      brief.textContent = info.brief;
      wrap.append(brief);
    }

    const goals = document.createElement("ul");
    goals.className = "challenge-goals";
    for (const g of active.goals) {
      const li = document.createElement("li");
      li.className = g.done ? "done" : "";
      li.textContent = g.label;
      goals.append(li);
    }
    wrap.append(goals);

    if (active.state !== "running") {
      const banner = document.createElement("div");
      banner.className = `challenge-banner ${active.state}`;
      banner.textContent =
        active.state === "passed"
          ? `Passed in ${fmtClock(active.elapsed_s)}`
          : `Failed${active.reason ? ` — ${active.reason}` : ""}`;
      wrap.append(banner);
    }

    if (guided()) {
      wrap.append(guidedActions());
      return wrap;
    }
    const actions = document.createElement("div");
    actions.className = "challenge-actions";
    if (active.state === "running") {
      actions.append(actionButton("Abort", () => session.abortChallenge(), "Stop the run and return to the challenge list"));
    } else {
      actions.append(
        actionButton("Retry", () => session.startChallenge(active.id), "Start the same challenge again"),
        actionButton("Done", () => session.abortChallenge(), "Dismiss the result and return to the list"),
      );
    }
    wrap.append(actions);
    return wrap;
  }

  /**
   * @param {string} label
   * @param {() => void} onClick
   * @param {string} [title]
   */
  function actionButton(label, onClick, title = "") {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "challenge-action";
    btn.textContent = label;
    if (title) btn.title = title;
    btn.addEventListener("click", onClick);
    return btn;
  }

  /** @param {any} active */
  function timerText(active) {
    if (active.state !== "running") return fmtClock(active.elapsed_s);
    if (active.time_limit_s != null) return `${fmtClock(Math.max(0, active.time_limit_s - active.elapsed_s))} left`;
    return fmtClock(active.elapsed_s);
  }

  /** @param {number} s */
  function fmtClock(s) {
    const m = Math.floor(s / 60);
    return `${m}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
  }

  return {
    dismiss() {
      if (!challengeRunning) setOpen(false);
    },
    destroy() {
      intro?.close();
      unsub();
      unsubEnvironment?.();
      unsubFirstRun?.();
      document.removeEventListener(PANEL_OPEN_EVENT, onPanelOpen);
      document.removeEventListener("pointerdown", onOutsidePointer, {capture:true});
      dock.remove();
    },
  };
}
