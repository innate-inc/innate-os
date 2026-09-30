// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Episode list — the episodes of the selected dataset as a rich table, from
// /brain/recorder/get_task_metadata. Two flavors sharing one component:
//
//   Training dataset (skill.type != "eval") — operator demonstrations:
//     Episode · Recorded · Duration · Label · actions. An episode promoted
//     from an evaluation keeps a "rollout" provenance badge.
//
//   Evaluation dataset (skill.type == "eval") — judged policy rollouts:
//     Episode · Policy · Recorded · Duration · Result · actions, plus a
//     success-rate summary and per-policy filter chips. Right-click a run (or
//     use the + action) to add it to a training dataset via CopyEpisode.
//
// Each episode is "ready" (has H.264 MP4s → click to replay) or "preparing"
// (the dataset_encoder is converting it; a "Prepare video" button can requeue).
// Thumbnails are lazily-loaded frames from the /episode/thumb route.

import { openPublishModal } from "./publishModal.js";
import {
  GET_TASK_METADATA_SERVICE,
  ENCODE_EPISODE_SERVICE,
  ENCODE_STATUS_TOPIC,
  SET_EPISODE_OUTCOME_SERVICE,
  DELETE_EPISODE_SERVICE,
} from "../constants.js";
import { openEpisodeMenu } from "./episodeMenu.js";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// Line-style icons, same stroke language as the rail (24×24, stroke currentColor).
const ICON_TRASH =
  '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16"/><path d="M10 11v6M14 11v6"/><path d="M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12"/><path d="M9 7V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v3"/></svg>';
const ICON_DOWNLOAD =
  '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 4v10"/><path d="M8 11l4 4 4-4"/><path d="M5 19h14"/></svg>';
const ICON_PLUS =
  '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>';

const TRAIN_COLS = ["Episode", "Recorded", "Duration", "Label", ""];
const EVAL_COLS = ["Run", "Policy", "Recorded", "Duration", "Result", ""];

/**
 * @param {HTMLElement} parent
 * @param {import("../rosClient.js").RosClient} ros
 * @param {{ onOpen: (skill: Skill, episode: EpisodeSummary) => void, getTargets: () => Skill[] }} opts
 * @returns {{ show: (skill: Skill) => void, neighbor: (ep: EpisodeSummary, delta: number) => EpisodeSummary | null, destroy: () => void }}
 */
export function createEpisodeList(parent, ros, opts) {
  const wrap = document.createElement("div");
  wrap.className = "episodes";

  // --- header: title + counts + toolbar -----------------------------------
  const head = document.createElement("div");
  head.className = "episodes-head";
  const headText = document.createElement("div");
  const titleRow = document.createElement("div");
  titleRow.className = "episodes-titlerow";
  const title = document.createElement("h2");
  title.className = "episodes-title";
  const kindBadge = document.createElement("span");
  kindBadge.className = "episodes-kind";
  kindBadge.textContent = "evaluation";
  kindBadge.title = "Policy rollouts recorded during evaluation — never used for training";
  kindBadge.hidden = true;
  titleRow.append(title, kindBadge);
  const sub = document.createElement("p");
  sub.className = "episodes-sub";
  headText.append(titleRow, sub);

  const tools = document.createElement("div");
  tools.className = "episodes-tools";
  const search = document.createElement("input");
  search.type = "search";
  search.className = "episodes-search";
  search.placeholder = "Search episodes";
  const sortBtn = document.createElement("button");
  sortBtn.type = "button";
  sortBtn.className = "episodes-tool";
  sortBtn.title = "Order episodes by recording time";
  const refreshBtn = document.createElement("button");
  refreshBtn.type = "button";
  refreshBtn.className = "episodes-tool";
  refreshBtn.textContent = "↻";
  refreshBtn.title = "Refresh";
  // Training datasets: jump to Collect to record more demonstrations.
  // Eval datasets: jump to Profiling to run more evaluations instead.
  const collectLink = document.createElement("a");
  collectLink.className = "episodes-tool episodes-collect";
  // Training datasets only: convert to a LeRobotDataset on the robot and upload it.
  const publishBtn = document.createElement("button");
  publishBtn.type = "button";
  publishBtn.className = "episodes-tool";
  publishBtn.textContent = "Publish to Hugging Face";
  publishBtn.title = "Convert this dataset to a LeRobotDataset and upload it to the Hugging Face Hub";
  publishBtn.addEventListener("click", () => {
    if (current) openPublishModal(wrap, current);
  });
  tools.append(search, sortBtn, refreshBtn, publishBtn, collectLink);
  head.append(headText, tools);

  // --- eval-only per-policy filter chips ------------------------------------
  const policyBar = document.createElement("div");
  policyBar.className = "policy-bar";
  policyBar.hidden = true;

  // --- column header + scrolling rows --------------------------------------
  const colhead = document.createElement("div");
  colhead.className = "episodes-colhead";

  const rowsEl = document.createElement("div");
  rowsEl.className = "episodes-rows";

  const foot = document.createElement("p");
  foot.className = "episodes-foot";

  wrap.append(head, policyBar, colhead, rowsEl, foot);
  parent.appendChild(wrap);

  let requestSeq = 0;
  /** @type {Skill | null} */
  let current = null;
  /** @type {EpisodeSummary[]} */
  let episodes = [];
  let dataFreq = 0;
  /** @type {EncodeStatusMsg | null} */
  let encodeStatus = null;
  let query = "";
  let sortNewest = true;
  /** @type {string | null} eval view: only show runs of this policy */
  let policyFilter = null;

  syncSortLabel();
  showIdle();

  const unsub = ros.subscribe(ENCODE_STATUS_TOPIC, (msg) => {
    if (!current || msg.task_directory !== current.directory) return;
    encodeStatus = msg;
    // Re-fetch metadata whenever this skill's encode completes — unconditionally,
    // not only if we witnessed the "encoding" phase (the topic isn't latched, so
    // a "done" can arrive without us having seen the in-progress updates). "done"
    // fires once per completion, so this is at most one extra fetch.
    if (msg.stage === "done") show(current);
    else render();
  }, undefined, "brain_messages/msg/EncodeStatus");

  search.addEventListener("input", () => {
    query = search.value.trim();
    render();
  });
  sortBtn.addEventListener("click", () => {
    sortNewest = !sortNewest;
    syncSortLabel();
    render();
  });
  refreshBtn.addEventListener("click", () => current && show(current));

  function isEval() {
    return current?.type === "eval";
  }

  function syncSortLabel() {
    sortBtn.textContent = `Sort: ${sortNewest ? "Newest" : "Oldest"}`;
  }

  /** @param {string} text */
  function showMessage(text) {
    const p = document.createElement("p");
    p.className = "datasets-empty";
    p.textContent = text;
    rowsEl.replaceChildren(p);
  }

  function showIdle() {
    title.textContent = "Datasets";
    kindBadge.hidden = true;
    sub.textContent = "Select a dataset to browse its episodes.";
    tools.hidden = true;
    colhead.hidden = true;
    policyBar.hidden = true;
    foot.textContent = "";
    showMessage(
      "Pick a dataset on the left — training data holds the operator's recorded demonstrations, evaluation runs hold judged policy rollouts.",
    );
  }

  function buildColhead() {
    colhead.replaceChildren();
    for (const label of isEval() ? EVAL_COLS : TRAIN_COLS) {
      const c = document.createElement("span");
      c.className = "col";
      c.textContent = label;
      colhead.appendChild(c);
    }
  }

  /** @param {Skill} skill */
  function show(skill) {
    const seq = ++requestSeq;
    current = skill;
    policyFilter = null;
    wrap.classList.toggle("episodes--eval", isEval());
    kindBadge.hidden = !isEval();
    buildColhead();
    tools.hidden = false;
    publishBtn.hidden = isEval();
    search.placeholder = isEval() ? "Search runs or policies" : "Search episodes";
    if (isEval()) {
      collectLink.href = "/profiling";
      collectLink.textContent = "▶ Evaluate";
      collectLink.title = "Run new policy rollouts on the Profiling page";
    } else {
      collectLink.href = `/collect?dir=${encodeURIComponent(skill.directory || "")}&name=${encodeURIComponent(skill.name)}`;
      collectLink.textContent = "+ Collect";
      collectLink.title = "Record new episodes for this dataset";
    }
    title.textContent = skill.name;
    sub.textContent = "loading…";
    showMessage(isEval() ? "Loading evaluation runs…" : "Loading episodes…");

    ros.callService(GET_TASK_METADATA_SERVICE, { task_directory: skill.directory })
      .then((res) => {
        if (seq !== requestSeq) return;
        if (!res?.success || !res.json_metadata) throw new Error(res?.message || "No metadata returned");
        /** @type {TaskSummary} */
        const meta = JSON.parse(res.json_metadata);
        episodes = Array.isArray(meta.episodes) ? meta.episodes : [];
        dataFreq = meta.data_frequency || 0;
        render();
      })
      .catch((err) => {
        if (seq !== requestSeq) return;
        episodes = [];
        colhead.hidden = true;
        policyBar.hidden = true;
        foot.textContent = "";
        sub.textContent = "error";
        showMessage(`Couldn't load episodes — ${err.message}`);
      });
  }

  /** An episode is playable only when ITS OWN MP4s exist. Do not fall back to
   * the dataset-level `dataset_type === "h264"`: that flips true after the first
   * batch is encoded, which would mark every newly-recorded (un-encoded) episode
   * "ready" before its video exists. The converter writes per-episode
   * video_files for everything it encodes, so this is the reliable signal.
   * @param {EpisodeSummary} ep */
  function isReady(ep) {
    return !!(ep.video_files && ep.video_files.length);
  }

  /** Two states: "ready" (has MP4s, playable) or "preparing" (recorded but not
   * yet encoded — auto-encoding runs in the background). @param {EpisodeSummary} ep
   * @returns {"ready"|"preparing"} */
  function stateOf(ep) {
    return isReady(ep) ? "ready" : "preparing";
  }

  /** Episodes in display order (current sort + search + policy filter). @returns {EpisodeSummary[]} */
  function orderedEpisodes() {
    const sorted = [...episodes].sort((a, b) => (sortNewest ? numericId(b) - numericId(a) : numericId(a) - numericId(b)));
    const q = query.toLowerCase();
    return sorted.filter((e) => {
      if (policyFilter !== null && (e.policy || "") !== policyFilter) return false;
      if (!q) return true;
      return String(numericId(e)).includes(q) || (e.policy || "").toLowerCase().includes(q);
    });
  }

  /** Nearest *ready* (playable) neighbor of *ep* in display order, walking in the
   * delta direction (-1 = up, +1 = below) and skipping still-preparing episodes.
   * Resolved against the live list, so it reflects deletes and encodes that
   * happened since the player opened — never returns a now-missing or un-encoded
   * episode (which would load a 404 video + "joint data unavailable").
   * @param {EpisodeSummary} ep @param {number} delta @returns {EpisodeSummary | null} */
  function neighbor(ep, delta) {
    const list = orderedEpisodes();
    let i = list.findIndex((e) => numericId(e) === numericId(ep));
    if (i < 0) return null; // ep itself was deleted while the player was open
    for (i += delta; i >= 0 && i < list.length; i += delta) {
      if (isReady(list[i])) return list[i];
    }
    return null;
  }

  /** Header summary. Training: ready/preparing counts. Eval: the running score
   * (✓/✗/unlabeled + success rate over labeled runs). */
  function renderSub() {
    sub.innerHTML = "";
    const n = episodes.length;
    if (!isEval()) {
      const readyCount = episodes.filter(isReady).length;
      const preparingCount = n - readyCount;
      sub.append(
        document.createTextNode(`${n} episode${n === 1 ? "" : "s"} · `),
        Object.assign(spanText(`${readyCount} ready`, "episodes-ready"), { title: "Encoded to H.264 — click a row to replay" }),
      );
      if (preparingCount > 0) {
        sub.append(document.createTextNode(" · "), spanText(`${preparingCount} preparing`, "episodes-preparing"));
      }
      return;
    }
    const ok = episodes.filter((e) => e.outcome === "success").length;
    const fail = episodes.filter((e) => e.outcome === "failure").length;
    const unlabeled = n - ok - fail;
    sub.append(document.createTextNode(`${n} run${n === 1 ? "" : "s"} · `));
    sub.append(spanText(`${ok}✓`, "episodes-ready"), document.createTextNode(" "));
    sub.append(spanText(`${fail}✗`, "episodes-fail"));
    if (ok + fail > 0) {
      sub.append(document.createTextNode(` · ${Math.round((100 * ok) / (ok + fail))}% success`));
    }
    if (unlabeled > 0) {
      sub.append(document.createTextNode(" · "), spanText(`${unlabeled} unlabeled`, "episodes-preparing"));
    }
  }

  /** Eval view: one filter chip per policy seen in this dataset, with its own
   * ✓/✗ tally — evaluations are compared per policy, so this is the primary
   * way to slice the list. */
  function renderPolicyBar() {
    if (!isEval()) {
      policyBar.hidden = true;
      return;
    }
    /** @type {Map<string, {ok: number, fail: number, n: number}>} */
    const byPolicy = new Map();
    for (const ep of episodes) {
      const key = ep.policy || "";
      const t = byPolicy.get(key) || { ok: 0, fail: 0, n: 0 };
      t.n++;
      if (ep.outcome === "success") t.ok++;
      else if (ep.outcome === "failure") t.fail++;
      byPolicy.set(key, t);
    }
    // A filter is only useful once there are ≥2 policies to tell apart.
    if (byPolicy.size < 2) {
      policyBar.hidden = true;
      if (policyFilter !== null) policyFilter = null;
      return;
    }
    policyBar.hidden = false;
    const frag = document.createDocumentFragment();
    const mk = (/** @type {string} */ label, /** @type {string | null} */ value, /** @type {string} */ tallyText) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "policy-chip" + (policyFilter === value ? " active" : "");
      const name = document.createElement("span");
      name.className = "policy-chip-name";
      name.textContent = label;
      chip.appendChild(name);
      if (tallyText) chip.appendChild(spanText(tallyText, "policy-chip-tally mono"));
      chip.title = value === null ? "Show all policies" : `Show only runs of ${label}`;
      chip.addEventListener("click", () => {
        policyFilter = policyFilter === value ? null : value;
        render();
      });
      frag.appendChild(chip);
    };
    mk("All policies", null, `${episodes.length}`);
    for (const [policy, t] of [...byPolicy.entries()].sort((a, b) => a[0].localeCompare(b[0]))) {
      mk(policy || "unknown", policy, `${t.ok}✓ ${t.fail}✗`);
    }
    policyBar.replaceChildren(frag);
  }

  function render() {
    if (!current) return;
    renderSub();
    renderPolicyBar();

    if (episodes.length === 0) {
      colhead.hidden = true;
      foot.textContent = "";
      showMessage(
        isEval()
          ? "No evaluation runs saved yet. Run a rollout from the Profiling page — every ✓/✗-labeled run lands here."
          : "This skill has no recorded episodes yet.",
      );
      return;
    }
    colhead.hidden = false;

    const shown = orderedEpisodes();

    const frag = document.createDocumentFragment();
    for (const ep of shown) {
      frag.appendChild(buildRow(ep));
    }
    rowsEl.replaceChildren(frag);
    foot.textContent = `Showing ${shown.length} of ${episodes.length} ${isEval() ? "runs" : "episodes"}`;
  }

  /** @param {EpisodeSummary} ep @returns {HTMLElement} */
  function buildRow(ep) {
    const state = stateOf(ep);
    const id = numericId(ep);
    const row = document.createElement("div");
    row.className = `episode-row episode-${state}`;

    // col 1: status dot + thumbnail + id
    const epCol = document.createElement("div");
    epCol.className = "ep-cell";
    const dot = document.createElement("span");
    dot.className = "ep-dot";

    const thumb = document.createElement("div");
    thumb.className = "ep-thumb";
    if (state === "ready") {
      const img = document.createElement("img");
      img.loading = "lazy"; // browser fetches only as rows scroll into view
      img.decoding = "async";
      img.alt = "";
      img.src = `/episode/thumb?dir=${encodeURIComponent(current?.directory || "")}&id=${id}&camera=camera_1`;
      img.addEventListener("error", () => thumb.classList.add("empty"));
      thumb.appendChild(img);
    }
    const idEl = document.createElement("span");
    idEl.className = "ep-id mono";
    idEl.textContent = `#${id}`;
    epCol.append(dot, thumb, idEl);
    // In a training dataset, an episode that came from a rollout (promoted from
    // an evaluation) or a replay is not an operator demonstration — badge it.
    if (!isEval() && ep.source && ep.source !== "teleop") {
      epCol.appendChild(sourceBadge(ep));
    }

    const recCol = document.createElement("span");
    recCol.className = "ep-recorded";
    recCol.textContent = formatRecorded(ep.start_time);
    recCol.title = ep.start_time || "";
    const durCol = document.createElement("span");
    durCol.className = "ep-duration mono";
    durCol.textContent = formatDuration(ep, dataFreq);

    // readiness is conveyed by the dot; replay on click
    const pct = encodeStatus ? Math.round((encodeStatus.progress || 0) * 100) : 0;
    dot.title = state === "ready" ? "ready" : encodeStatus?.stage === "encoding" ? `encoding ${pct}%` : "preparing";
    if (state === "ready") {
      row.classList.add("clickable");
      row.title = "Replay episode";
      row.addEventListener("click", () => opts.onOpen(/** @type {Skill} */ (current), ep));
    }

    // actions — manual re-encode kick (preparing only) + eval promote + delete
    const actions = document.createElement("div");
    actions.className = "ep-actions";
    if (state === "preparing") {
      const prep = actionBtn(ICON_DOWNLOAD, "Prepare video", (e) => {
        e.stopPropagation();
        prep.disabled = true;
        ros.callService(ENCODE_EPISODE_SERVICE, { task_directory: current?.directory, episode_id: id }).catch(
          () => (prep.disabled = false),
        );
      });
      actions.appendChild(prep);
    }
    if (isEval()) {
      // The + is a single-purpose "add to…" picker — Replay/Delete live on the
      // row and in the right-click menu, so repeating them here reads wrong.
      const add = actionBtn(ICON_PLUS, "Add to training dataset", (e) => {
        e.stopPropagation();
        const r = add.getBoundingClientRect();
        openEpisodeMenu({
          x: r.left,
          y: r.bottom + 4,
          ros,
          sourceDir: current?.directory || "",
          episodeId: id,
          targets: opts.getTargets(),
        });
      });
      actions.appendChild(add);
    }
    actions.appendChild(
      actionBtn(ICON_TRASH, "Delete episode", (e) => {
        e.stopPropagation();
        deleteEpisode(ep, id);
      }),
    );

    if (isEval()) {
      const polCol = document.createElement("span");
      polCol.className = "ep-policy mono";
      polCol.textContent = ep.policy || "—";
      if (ep.policy) polCol.title = ep.policy;

      row.append(epCol, polCol, recCol, durCol, buildResultCell(ep), actions);
      // The whole point of keeping eval runs: judge them, then promote the
      // good ones. Right-click = the full episode menu.
      row.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        openMenu(ep, id, e.clientX, e.clientY, state === "ready");
      });
    } else {
      row.append(epCol, recCol, durCol, buildLabelSelect(ep), actions);
    }
    return row;
  }

  /** @param {EpisodeSummary} ep @param {number} id @param {number} x @param {number} y @param {boolean} ready */
  function openMenu(ep, id, x, y, ready) {
    openEpisodeMenu({
      x,
      y,
      ros,
      sourceDir: current?.directory || "",
      episodeId: id,
      targets: opts.getTargets(),
      onReplay: ready ? () => opts.onOpen(/** @type {Skill} */ (current), ep) : null,
      onDelete: () => deleteEpisode(ep, id),
    });
  }

  /** Provenance badge for non-teleop episodes in a training dataset.
   * @param {EpisodeSummary} ep */
  function sourceBadge(ep) {
    const badge = document.createElement("span");
    badge.className = `ep-src ep-src-${ep.source}`;
    badge.textContent = ep.source === "rollout" ? "rollout" : "replay";
    badge.title =
      ep.source === "rollout"
        ? `Recorded during a policy rollout${ep.policy ? ` of ${ep.policy}` : ""} — added from an evaluation`
        : "Recorded by replaying a saved trajectory";
    return badge;
  }

  /** Eval result cell: outcome dropdown (with an explicit Unlabeled state — a
   * run saved but never judged must not read as a success) + failure-mode tags.
   * @param {EpisodeSummary} ep @returns {HTMLElement} */
  function buildResultCell(ep) {
    const cell = document.createElement("div");
    cell.className = "ep-result";
    const sel = document.createElement("select");
    sel.title = `Judge this rollout — ${SET_EPISODE_OUTCOME_SERVICE}`;
    const stateOfOutcome = () => (ep.outcome === "success" ? "is-success" : ep.outcome === "failure" ? "is-fail" : "is-unlabeled");
    const applyClass = () => (sel.className = `ep-label-select ${stateOfOutcome()}`);
    for (const [val, text] of [
      ["", "Unlabeled"],
      ["success", "✓ Success"],
      ["failure", "✗ Failure"],
    ]) {
      const o = document.createElement("option");
      o.value = val;
      o.textContent = text;
      if (val === (ep.outcome || "")) o.selected = true;
      sel.appendChild(o);
    }
    applyClass();
    sel.addEventListener("click", (e) => e.stopPropagation());
    sel.addEventListener("change", (e) => {
      e.stopPropagation();
      const outcome = /** @type {""|"success"|"failure"} */ (sel.value);
      ep.outcome = outcome;
      applyClass();
      renderSub(); // keep the header score in step with the relabel
      renderPolicyBar();
      ros.callService(SET_EPISODE_OUTCOME_SERVICE, {
        task_directory: current?.directory,
        episode_id: numericId(ep),
        outcome,
        tags: [],
      }).catch((err) => console.error("[datasets] set outcome failed:", err));
    });
    cell.appendChild(sel);
    if (Array.isArray(ep.tags) && ep.tags.length) {
      const tags = document.createElement("span");
      tags.className = "ep-tags";
      tags.title = ep.tags.join(", ");
      for (const t of ep.tags.slice(0, 2)) tags.appendChild(spanText(t, "ep-tag"));
      if (ep.tags.length > 2) tags.appendChild(spanText(`+${ep.tags.length - 2}`, "ep-tag more"));
      cell.appendChild(tags);
    }
    return cell;
  }

  /** Training label dropdown: Successful (default) / Unsuccessful.
   * @param {EpisodeSummary} ep @returns {HTMLElement} */
  function buildLabelSelect(ep) {
    const cell = document.createElement("div");
    cell.className = "ep-label";
    const sel = document.createElement("select");
    sel.title = `Label this demonstration — ${SET_EPISODE_OUTCOME_SERVICE}`;
    const applyClass = (/** @type {boolean} */ fail) =>
      (sel.className = `ep-label-select ${fail ? "is-fail" : "is-success"}`);
    applyClass(ep.outcome === "failure");
    for (const [val, text] of [
      ["success", "Successful"],
      ["failure", "Unsuccessful"],
    ]) {
      const o = document.createElement("option");
      o.value = val;
      o.textContent = text;
      if ((val === "failure") === (ep.outcome === "failure")) o.selected = true;
      sel.appendChild(o);
    }
    sel.addEventListener("click", (e) => e.stopPropagation());
    sel.addEventListener("change", (e) => {
      e.stopPropagation();
      const outcome = /** @type {"success"|"failure"} */ (sel.value);
      ep.outcome = outcome;
      applyClass(outcome === "failure");
      ros.callService(SET_EPISODE_OUTCOME_SERVICE, {
        task_directory: current?.directory,
        episode_id: numericId(ep),
        outcome,
        tags: [],
      }).catch((err) => console.error("[datasets] set outcome failed:", err));
    });
    cell.appendChild(sel);
    return cell;
  }

  /** @param {EpisodeSummary} ep @param {number} id */
  function deleteEpisode(ep, id) {
    if (!current) return;
    if (!window.confirm(`Delete episode #${id}? This permanently removes its video and data.`)) return;
    ros.callService(DELETE_EPISODE_SERVICE, { task_directory: current.directory, episode_id: id })
      .then((res) => {
        if (res && res.success === false) throw new Error(res.message || "delete failed");
        episodes = episodes.filter((e) => e !== ep);
        render();
      })
      .catch((err) => window.alert(`Couldn't delete episode: ${err.message}`));
  }

  return {
    show,
    neighbor,
    destroy() {
      requestSeq++;
      unsub();
      wrap.remove();
    },
  };
}

/** @param {string} iconSvg @param {string} title @param {(e: Event) => void} onClick @returns {HTMLButtonElement} */
function actionBtn(iconSvg, title, onClick) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "ep-action-btn" + (title === "Delete episode" ? " ep-delete" : "");
  b.title = title;
  b.innerHTML = iconSvg;
  b.addEventListener("click", onClick);
  return b;
}

/** @param {string} text @param {string} cls */
function spanText(text, cls) {
  const s = document.createElement("span");
  s.className = cls;
  s.textContent = text;
  return s;
}

/** Numeric episode index from "episode_0" or "episode_0.h5". @param {EpisodeSummary} ep */
function numericId(ep) {
  const m = /(\d+)/.exec(String(ep.episode_id)) || /(\d+)/.exec(ep.file_name || "");
  return m ? Number(m[1]) : 0;
}

/** "2026-04-12T20:25:31" → "12 Apr, 20:25". @param {string} iso */
function formatRecorded(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso || "—";
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${d.getDate()} ${MONTHS[d.getMonth()]}, ${hh}:${mm}`;
}

/** Episode duration: prefer num_timesteps ÷ data_frequency, else timestamp diff.
 * @param {EpisodeSummary} ep @param {number} hz */
function formatDuration(ep, hz) {
  let secs = NaN;
  if (hz && ep.num_timesteps) {
    secs = ep.num_timesteps / hz;
  } else {
    const t0 = new Date(ep.start_time).getTime();
    const t1 = new Date(ep.end_time).getTime();
    if (!Number.isNaN(t0) && !Number.isNaN(t1) && t1 >= t0) secs = (t1 - t0) / 1000;
  }
  if (Number.isNaN(secs)) return "—";
  if (secs < 60) return `${secs.toFixed(1)}s`;
  const m = Math.floor(secs / 60);
  const s = Math.round(secs % 60);
  return `${m}m ${s}s`;
}
