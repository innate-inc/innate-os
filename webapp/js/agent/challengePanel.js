// @ts-check
// Challenge panel (sim only) — a stage launcher beside Scene Setup. Renders the
// world server's challenge judge state relayed through the sim session
// (session.onChallenge, see challenges.py): a roster of challenges to start,
// and while one runs, its goal checklist, timer, and pass/fail banner. All
// judging happens server-side against ground truth; this panel is a thin
// renderer plus an explicit preview → run flow. The chat owns prompt delivery.

import { startChallengeAndWait } from "./challengeRun.js";
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
 * @param {{run: (id: string, prompt: string, signal: AbortSignal) => Promise<void>, stop: () => Promise<void>}} actions
 * @returns {{ destroy: () => void, dismiss: () => void }}
 */
export function createChallengePanel(root, session, actions) {
  const dock = document.createElement("div");
  dock.className = "agent-challenge-dock";
  dock.hidden = true;

  const panel = document.createElement("section");
  panel.id = "agent-challenge-panel";
  panel.className = "challenge-panel";

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
    open = next;
    dock.classList.toggle("open", next);
    launcher.setAttribute("aria-expanded", String(next));
    launcher.setAttribute("aria-label", next ? "Close challenges" : "Open challenges");
    hidePromptPreview();
    if (!next) return;
    document.dispatchEvent(new CustomEvent(PANEL_OPEN_EVENT, { detail: { panel: PANEL_ID } }));
    if (revealed) return;
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
    if (event.target instanceof Node && !preview.contains(event.target) && !previewOwner?.contains(event.target)) hidePromptPreview();
    if (!open || !coarsePointer.matches || challengeRunning) return;
    if (event.target instanceof Node && (dock.contains(event.target) || preview.contains(event.target))) return;
    setOpen(false);
  };
  document.addEventListener("pointerdown", onOutsidePointer, true);
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

  // Outside the scrolling panel so a long prompt is never clipped by its edge.
  const preview = document.createElement("div");
  preview.id = `${panel.id}-prompt-preview`;
  preview.className = "challenge-prompt-preview";
  preview.setAttribute("role", "tooltip");
  preview.hidden = true;
  const previewLabel = document.createElement("div");
  previewLabel.className = "challenge-prompt-label";
  previewLabel.textContent = "Prompt to send";
  const previewText = document.createElement("div");
  previewText.className = "challenge-prompt-preview-text";
  const previewHint = document.createElement("div");
  previewHint.className = "challenge-prompt-preview-hint";
  previewHint.textContent = "Select text to copy";
  preview.append(previewLabel, previewText, previewHint);
  document.body.append(preview);
  /** @type {HTMLElement | null} */
  let previewOwner = null;
  /** @type {ReturnType<typeof setTimeout> | undefined} */
  let previewTimer;

  function hidePromptPreview() {
    clearTimeout(previewTimer);
    preview.hidden = true;
    previewOwner?.removeAttribute("aria-describedby");
    previewOwner = null;
  }
  function leavePromptPreview() {
    clearTimeout(previewTimer);
    // Give the pointer time to cross the small gap into the preview to read,
    // scroll or select a long prompt. Keyboard focus keeps its preview open.
    previewTimer = setTimeout(() => {
      if (previewOwner === document.activeElement && previewOwner?.matches(":focus-visible")) return;
      const selection = document.getSelection();
      if (selection && !selection.isCollapsed && preview.contains(selection.anchorNode)) return;
      hidePromptPreview();
    }, 120);
  }
  /** @param {HTMLElement} owner @param {() => string} getPrompt @param {() => boolean} [canShow] */
  function attachPromptPreview(owner, getPrompt, canShow = () => true) {
    const show = () => {
      hidePromptPreview();
      if (!open || !canShow()) return;
      previewOwner = owner;
      previewText.textContent = getPrompt().trim() || "This prompt is empty. Add instructions before running.";
      owner.setAttribute("aria-describedby", preview.id);
      const rect = owner.getBoundingClientRect();
      const rightSpace = window.innerWidth - rect.right - 24;
      const beside = rightSpace >= 240;
      preview.style.width = `${Math.min(320, beside ? rightSpace : window.innerWidth - 24)}px`;
      preview.hidden = false;
      const height = preview.offsetHeight;
      const left = beside ? rect.right + 10 : Math.min(rect.left, window.innerWidth - preview.offsetWidth - 12);
      const top = beside ? rect.top : rect.top >= height + 22 ? rect.top - height - 10 : rect.bottom + 10;
      preview.style.left = `${Math.max(12, left)}px`;
      preview.style.top = `${Math.max(12, Math.min(top, window.innerHeight - height - 12))}px`;
    };
    owner.addEventListener("pointerenter", (event) => {
      if (event.pointerType !== "touch") show();
    });
    owner.addEventListener("pointerleave", leavePromptPreview);
    owner.addEventListener("focus", () => { if (owner.matches(":focus-visible")) show(); });
    owner.addEventListener("blur", hidePromptPreview);
    owner.addEventListener("pointerdown", hidePromptPreview);
  }
  const dismissPreviewOnEscape = (/** @type {KeyboardEvent} */ event) => {
    if (event.key === "Escape" && !preview.hidden) {
      event.preventDefault();
      event.stopPropagation();
      hidePromptPreview();
    }
  };
  preview.addEventListener("pointerenter", () => clearTimeout(previewTimer));
  preview.addEventListener("pointerleave", leavePromptPreview);
  panel.addEventListener("scroll", hidePromptPreview);
  window.addEventListener("resize", hidePromptPreview);
  document.addEventListener("keydown", dismissPreviewOnEscape);
  panel.append(head, body);
  dock.append(panel, launcher);
  root.appendChild(dock);
  setOpen(false);

  /** Last rendered structure (block minus the ticking clock) — the timer text
   * updates in place so the DOM isn't rebuilt 10x a second. */
  let renderedKey = "";
  /** @type {HTMLElement | null} */
  let timerEl = null;

  /** @type {any} */
  let latest = { list: [], active: null };
  let selected = "";
  let lastAttempt = "";
  let busy = false;
  let busyLabel = "Starting…";
  let error = "";
  let sentAttempt = "";
  let sentPrompt = "";
  let destroyed = false;
  /** @type {AbortController | null} */
  let launch = null;
  const expandedPrompts = new Set();

  const unsub = session.onChallenge((/** @type {any} */ block) => {
    dock.hidden = false;
    latest = block;
    const active = block.active;
    challengeRunning = active?.state === "running";
    dock.classList.toggle("active", challengeRunning);
    const attempt = active?.attempt_id ?? active?.id ?? "";
    if (attempt && attempt !== lastAttempt) selected = active.id;
    lastAttempt = attempt;
    if (selected && !block.list.some((/** @type {any} */ c) => c.id === selected)) {
      selected = "";
      launch?.abort();
      error = "";
    }
    if (timerEl && active) timerEl.textContent = timerText(active);
    // Only structure changes rebuild the panel. Motion, story data and narrator
    // messages must not replace a focused button every tick.
    const key = JSON.stringify([block.list, active && [active.id, attempt, active.state, active.reason, active.goals]]);
    if (key === renderedKey) return;
    renderedKey = key;
    render();
  });

  function render() {
    if (destroyed) return;
    hidePromptPreview();
    timerEl = null;
    const info = latest.list.find((/** @type {any} */ c) => c.id === selected);
    body.replaceChildren(info ? renderDetail(info) : renderList(latest.list));
  }

  /** @param {any[]} list */
  function renderList(list) {
    const wrap = document.createElement("div");
    wrap.className = "challenge-list";
    const listed = list.filter((/** @type {any} */ c) => c.listed !== false);
    if (listed.length) {
      const hint = document.createElement("p");
      hint.className = "challenge-preview-hint";
      hint.textContent = coarsePointer.matches ? "Select a challenge to see its goals." : "Hover to preview the prompt · Select for details";
      wrap.append(hint);
    }
    if (!listed.length) {
      const empty = document.createElement("div");
      empty.className = "challenge-empty";
      empty.textContent = "No challenges in this environment yet.";
      wrap.append(empty);
    }
    for (const c of listed) {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "challenge-item";
      item.disabled = busy;
      const dot = document.createElement("span");
      dot.className = `challenge-dot${c.passed ? " passed" : ""}`;
      dot.setAttribute("aria-hidden", "true");
      const text = document.createElement("div");
      text.className = "challenge-item-text";
      const name = document.createElement("div");
      name.className = "challenge-item-title";
      name.textContent = c.title;
      const meta = document.createElement("div");
      meta.className = "challenge-item-meta";
      meta.textContent = latest.active?.id === c.id && challengeRunning ? "In progress"
        : c.passed ? `Passed${c.best_time_s != null ? ` · best ${fmtClock(c.best_time_s)}` : ""}`
        : c.attempts ? `${c.attempts} ${c.attempts === 1 ? "attempt" : "attempts"}`
        : c.time_limit_s ? `${fmtClock(c.time_limit_s)} time limit` : "Ready to try";
      text.append(name, meta);
      const arrow = document.createElement("span");
      arrow.className = "challenge-play";
      arrow.textContent = "›";
      arrow.setAttribute("aria-hidden", "true");
      item.append(dot, text, arrow);
      attachPromptPreview(item, () => c.prompt ?? c.brief);
      item.addEventListener("click", () => {
        selected = c.id;
        error = "";
        render();
        /** @type {HTMLElement | null} */ (body.querySelector(".challenge-back"))?.focus();
      });
      wrap.append(item);
    }
    return wrap;
  }

  /** @param {any} info */
  function renderDetail(info) {
    const active = latest.active?.id === info.id ? latest.active : null;
    const running = active?.state === "running";
    const wrap = document.createElement("div");
    wrap.className = "challenge-active";
    const back = actionButton("‹ All challenges", () => {
      selected = "";
      error = "";
      render();
      /** @type {HTMLElement | null} */ (body.querySelector(".challenge-item"))?.focus();
    });
    back.className = "challenge-back";
    wrap.append(back);

    const titleRow = document.createElement("div");
    titleRow.className = "challenge-title-row";
    const name = document.createElement("h3");
    name.className = "challenge-detail-title";
    name.textContent = info.title;
    const time = document.createElement("span");
    time.className = "challenge-timer";
    if (active) {
      timerEl = time;
      time.textContent = timerText(active);
      time.classList.toggle("final", !running);
    } else time.textContent = info.time_limit_s ? `${fmtClock(info.time_limit_s)} limit` : "No time limit";
    titleRow.append(name, time);
    wrap.append(titleRow);

    if (active && !running) {
      const banner = document.createElement("div");
      banner.className = `challenge-banner ${active.state}`;
      banner.setAttribute("role", "status");
      banner.textContent = active.state === "passed" ? `Passed in ${fmtClock(active.elapsed_s)}`
        : `Failed${active.reason ? ` — ${active.reason}` : ""}`;
      wrap.append(banner);
    }

    const prompt = info.prompt ?? info.brief;
    const goals = document.createElement("ul");
    goals.className = "challenge-goals";
    for (const g of active?.goals ?? (info.goals ?? []).map((/** @type {string} */ label) => ({ label, done: false }))) {
      const li = document.createElement("li");
      li.className = g.done ? "done" : "";
      li.textContent = g.label;
      goals.append(li);
    }
    if (goals.childElementCount) wrap.append(goals);

    const promptDetails = document.createElement("details");
    promptDetails.className = "challenge-prompt";
    promptDetails.open = expandedPrompts.has(info.id);
    const promptSummary = document.createElement("summary");
    promptSummary.textContent = running && active.attempt_id === sentAttempt ? "View sent prompt" : "View prompt";
    const promptText = document.createElement("p");
    promptText.textContent = running && active.attempt_id === sentAttempt ? sentPrompt : prompt;
    attachPromptPreview(promptSummary, () => promptText.textContent ?? prompt, () => !promptDetails.open);
    promptDetails.append(promptSummary, promptText);
    promptDetails.addEventListener("toggle", () => {
      if (!promptDetails.isConnected) return;
      if (promptDetails.open) expandedPrompts.add(info.id);
      else expandedPrompts.delete(info.id);
    });
    wrap.append(promptDetails);

    const footer = document.createElement("div");
    footer.className = "challenge-launch";
    if (running) {
      const sent = document.createElement("div");
      sent.className = "challenge-sent";
      sent.textContent = active.attempt_id === sentAttempt ? "✓ Prompt sent" : "Challenge in progress";
      sent.setAttribute("role", "status");
      footer.append(sent);
    } else {
      const run = actionButton(busy ? busyLabel : active ? "Run again" : "Run challenge", () => void start(info, prompt, false));
      run.className = "challenge-run";
      run.setAttribute("aria-busy", String(busy));
      const icon = document.createElement("span");
      icon.className = busy ? "challenge-run-spinner" : "challenge-run-icon";
      icon.setAttribute("aria-hidden", "true");
      run.prepend(icon);
      run.disabled = busy || !prompt.trim();
      attachPromptPreview(run, () => prompt);
      footer.append(run);
    }
    const secondaryRow = document.createElement("div");
    secondaryRow.className = "challenge-secondary-row";
    const secondary = actionButton(busy && busyLabel === "Stopping…" ? "Stopping…" : running ? "Stop challenge" : "Try manually", () => {
      if (running) void stop();
      else void start(info, prompt, true);
    });
    secondary.className = running ? "challenge-stop" : "challenge-manual";
    secondary.setAttribute("aria-busy", String(busy && busyLabel === "Stopping…"));
    secondaryRow.append(secondary);
    footer.append(secondaryRow);
    const notice = document.createElement("div");
    notice.className = "challenge-launch-error";
    notice.setAttribute("role", "alert");
    notice.textContent = error;
    notice.hidden = !error;
    footer.append(notice);
    wrap.append(footer);
    return wrap;
  }

  /** @param {any} info @param {string} prompt @param {boolean} manual */
  async function start(info, prompt, manual) {
    if (busy || destroyed || (!manual && !prompt.trim())) return;
    selected = info.id;
    busy = true;
    busyLabel = "Starting…";
    error = "";
    launch = new AbortController();
    render();
    try {
      if (manual) {
        await actions.stop();
        launch.signal.throwIfAborted();
        await startChallengeAndWait(session, info.id, launch.signal);
      }
      else {
        await actions.run(info.id, prompt.trim(), launch.signal);
        sentAttempt = latest.active?.attempt_id ?? "";
        sentPrompt = prompt.trim();
      }
    } catch (cause) {
      if (!launch.signal.aborted) {
        error = cause instanceof Error ? cause.message : "Could not start the challenge. Try again.";
        setOpen(true);
      }
    } finally {
      busy = false;
      launch = null;
      render();
    }
  }

  async function stop() {
    if (busy) return;
    busy = true;
    busyLabel = "Stopping…";
    error = "";
    render();
    try { await actions.stop(); }
    catch (cause) { error = cause instanceof Error ? cause.message : "Could not stop the challenge."; }
    finally { busy = false; render(); }
  }

  /** @param {string} label @param {() => void} onClick */
  function actionButton(label, onClick) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "challenge-action";
    btn.textContent = label;
    btn.disabled = busy;
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
      destroyed = true;
      launch?.abort();
      intro?.close();
      unsub();
      document.removeEventListener(PANEL_OPEN_EVENT, onPanelOpen);
      document.removeEventListener("pointerdown", onOutsidePointer, true);
      document.removeEventListener("keydown", dismissPreviewOnEscape);
      window.removeEventListener("resize", hidePromptPreview);
      hidePromptPreview();
      preview.remove();
      dock.remove();
    },
  };
}
