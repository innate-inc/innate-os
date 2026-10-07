// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// The model's side of the run: the phase ladder it inferred, and a newest-first
// feed of every tool call, decision and outcome. A step row shows the frames the
// model was looking at when it decided, so a wrong move can be read against what
// it actually saw rather than against the live camera seconds later.

const fmt = (/** @type {number} */ v, n = 3) => (Number.isFinite(v) ? v.toFixed(n) : "—");
const secs = (/** @type {number} */ v) => (Number.isFinite(v) && v > 0 ? `${v.toFixed(1)}s` : "");

/** Execution statuses that mean the arm did NOT do what was asked. */
const BAD_STATUS = new Set(["unreachable", "not_reached", "rejected", "stale_observation"]);
const OK_STATUS = new Set(["reached", "released", "closed"]);
/** Grasping and releasing are the spine of a multi-stage task, so they read as events. */
const GRIP_ACTIONS = new Set(["open_gripper", "close_gripper"]);

/** @param {any} decision */
function actionSummary(decision) {
  const action = decision?.action ?? "?";
  const pose = decision?.pose ?? [];
  if (action === "joint_step" && pose.length === 2) return `joint_step  j${pose[0]} ${pose[1] > 0 ? "+" : ""}${fmt(pose[1], 2)}`;
  if (action === "base_step" && pose.length === 1) return `base_step  ${fmt(pose[0], 3)} m`;
  if (action === "move" && pose.length === 6) return `move  ${pose.slice(0, 3).map((/** @type {number} */ v) => fmt(v)).join(" ")}`;
  if (action === "open_gripper") return "open gripper — release";
  if (action === "close_gripper") return "close gripper — grasp";
  return action;
}

/**
 * @param {HTMLElement} parent
 * @returns {{ el: HTMLElement, render: (run: any) => void, note: (text: string) => void, destroy: () => void }}
 */
export function createReasoningFeed(parent) {
  const el = document.createElement("section");
  el.className = "icl-panel icl-reason";
  el.innerHTML = `
    <header class="icl-panel-head">
      <h2>Reasoning</h2>
      <span class="icl-run-id mono microlabel"></span>
    </header>
    <ol class="icl-phases"></ol>
    <div class="icl-feed" role="log" aria-live="polite" aria-label="Model decisions"></div>`;

  const $ = (/** @type {string} */ s) => /** @type {HTMLElement} */ (el.querySelector(s));
  const runId = $(".icl-run-id");
  const phases = $(".icl-phases");
  const feed = $(".icl-feed");
  /** Rows already built, keyed by feed position, so a repaint doesn't rebuild images. */
  let painted = 0;
  let lastRun = "";

  /** @param {any} run */
  function renderPhases(run) {
    if (!run.phases?.length) {
      phases.replaceChildren();
      phases.hidden = true;
      return;
    }
    phases.hidden = false;
    phases.replaceChildren(
      ...run.phases.map((/** @type {any} */ p, /** @type {number} */ i) => {
        const li = document.createElement("li");
        li.className = "icl-phase";
        li.classList.toggle("is-current", i === run.phase);
        li.classList.toggle("is-done", i < run.phase);
        const name = document.createElement("span");
        name.className = "icl-phase-name";
        name.textContent = `${i + 1}. ${p.name}`;
        const span = document.createElement("span");
        span.className = "icl-phase-span mono microlabel";
        span.textContent = `${p.start_frame}–${p.end_frame} · ref ${p.reference_frame}`;
        li.append(name, span);
        li.title = p.advance_when ?? "";
        return li;
      }),
    );
  }

  /** @param {any} entry */
  function buildRow(entry) {
    const row = document.createElement("article");
    row.className = `icl-row is-${entry.kind}`;
    if (entry.kind === "step" && GRIP_ACTIONS.has(entry.decision?.action)) row.classList.add("is-grip");

    if (entry.kind === "tool") {
      const args = JSON.stringify(entry.arguments ?? {});
      row.innerHTML = `<div class="icl-row-head"><span class="icl-tag">${entry.tool}</span>
        <span class="icl-lat mono microlabel">${secs(entry.latency)}</span></div>`;
      const body = document.createElement("p");
      body.className = "icl-row-body mono";
      body.textContent = args.length > 400 ? `${args.slice(0, 400)}…` : args;
      row.appendChild(body);
      return row;
    }
    if (entry.kind === "phases") {
      row.innerHTML = `<div class="icl-row-head"><span class="icl-tag">record_phases</span></div>`;
      const body = document.createElement("p");
      body.className = "icl-row-body";
      body.textContent = (entry.phases ?? []).map((/** @type {any} */ p) => p.name).join(" → ");
      row.appendChild(body);
      return row;
    }
    if (entry.kind === "note" || entry.kind === "start" || entry.kind === "end") {
      const text =
        entry.kind === "note"
          ? entry.text
          : entry.kind === "start"
            ? "Run started"
            : `${entry.cancelled ? "Stopped" : entry.ok ? "Completed" : "Failed"} — ${entry.message}`;
      row.innerHTML = `<div class="icl-row-head"><span class="icl-tag">${entry.kind}</span></div>`;
      const body = document.createElement("p");
      body.className = "icl-row-body";
      body.textContent = text;
      row.appendChild(body);
      return row;
    }
    if (entry.kind === "execution") {
      row.innerHTML = `<div class="icl-row-head"><span class="icl-tag">${entry.execution?.status ?? "execution"}</span></div>`;
      const body = document.createElement("p");
      body.className = "icl-row-body";
      body.textContent = entry.execution?.reason ?? "";
      row.appendChild(body);
      return row;
    }

    // A decision.
    const head = document.createElement("div");
    head.className = "icl-row-head";
    const num = document.createElement("span");
    num.className = "icl-step mono";
    num.textContent = `#${entry.step}`;
    const tag = document.createElement("span");
    tag.className = "icl-tag mono";
    tag.textContent = actionSummary(entry.decision);
    const meta = document.createElement("span");
    meta.className = "icl-lat mono microlabel";
    const batch = entry.batch && !entry.batch.new ? ` · from batch (${entry.batch.remaining} left)` : "";
    const grip = typeof entry.observation?.holding === "boolean" ? ` · ${entry.observation.holding ? "holding" : "empty"}` : "";
    meta.textContent = `phase ${entry.phase + 1}${secs(entry.latency) ? ` · ${secs(entry.latency)}` : ""}${grip}${batch}`;
    head.append(num, tag, meta);
    row.appendChild(head);

    const why = document.createElement("p");
    why.className = "icl-row-body";
    why.textContent = entry.decision?.reason ?? "";
    row.appendChild(why);

    if (entry.decision?.evidence) {
      const evidence = document.createElement("p");
      evidence.className = "icl-row-body is-evidence";
      evidence.textContent = entry.decision.evidence;
      row.appendChild(evidence);
    }

    if (entry.images) {
      const shots = document.createElement("div");
      shots.className = "icl-row-shots";
      for (const name of ["head", "wrist"]) {
        if (!entry.images[name]) continue;
        const img = document.createElement("img");
        img.loading = "lazy";
        img.src = `data:image/jpeg;base64,${entry.images[name]}`;
        img.alt = `${name} camera the model saw at step ${entry.step}`;
        img.title = `${name} — what the model saw for this decision`;
        shots.appendChild(img);
      }
      row.appendChild(shots);
    }

    const outcome = document.createElement("p");
    outcome.className = "icl-outcome mono microlabel";
    row.appendChild(outcome);
    row.dataset.step = String(entry.step);
    return row;
  }

  /** @param {HTMLElement} row @param {any} entry */
  function paintOutcome(row, entry) {
    const outcome = /** @type {HTMLElement | null} */ (row.querySelector(".icl-outcome"));
    if (!outcome) return;
    const status = entry.execution?.status;
    if (!status) {
      outcome.textContent = entry.decision?.action === "observe" ? "" : "issued…";
      outcome.classList.remove("is-bad", "is-ok");
      return;
    }
    const error = entry.execution?.position_error_m ?? entry.execution?.joint_error_rad;
    outcome.textContent = `${status}${Number.isFinite(error) ? ` · off by ${fmt(error)}` : ""}`;
    outcome.classList.toggle("is-bad", BAD_STATUS.has(status));
    outcome.classList.toggle("is-ok", OK_STATUS.has(status));
    row.classList.toggle("is-failed", BAD_STATUS.has(status));
  }

  /** @param {any} run */
  function render(run) {
    if (run.id !== lastRun) {
      lastRun = run.id;
      painted = 0;
      feed.replaceChildren();
    }
    runId.textContent = run.id ? `${run.skill} · ${run.id.slice(0, 12)} · ${run.model}` : "no run seen yet";
    renderPhases(run);
    // Newest first: prepend the rows that arrived since the last paint.
    for (let i = painted; i < run.feed.length; i += 1) {
      const row = buildRow(run.feed[i]);
      if (run.feed[i].kind === "step") paintOutcome(row, run.feed[i]);
      feed.prepend(row);
    }
    painted = run.feed.length;
    // An execution that landed after its step row was painted.
    for (const entry of run.feed) {
      if (entry.kind !== "step" || !entry.execution) continue;
      const row = /** @type {HTMLElement | null} */ (feed.querySelector(`[data-step="${entry.step}"]`));
      if (row) paintOutcome(row, entry);
    }
  }

  /** A line from the skill's own feedback channel, not the model. */
  function note(text) {
    const row = document.createElement("article");
    row.className = "icl-row is-feedback";
    row.innerHTML = `<div class="icl-row-head"><span class="icl-tag">skill</span></div>`;
    const body = document.createElement("p");
    body.className = "icl-row-body";
    body.textContent = text;
    row.appendChild(body);
    feed.prepend(row);
  }

  parent.appendChild(el);
  return { el, render, note, destroy: () => el.remove() };
}
