// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// The demonstration picker and its context preview.
//
// "What the model is given" is not a reconstruction here: /icl/demonstration
// runs the same innate.gesture.Gesture loader the skill runs, so the frames and
// fields this panel shows are the ones that go into the request. The overview
// (no ?frames=) is turn one's context — even samples across the episode plus
// every significant gripper transition; a phase-aware run then pulls more
// frames on demand with inspect_demo, which the reasoning feed reports.

const LIST_URL = "/icl/demonstrations";
const DETAIL_URL = "/icl/demonstration";

/** Fields Gesture.context() serialises beside each pair of images. */
const CONTEXT_FIELDS = ["index", "time_s", "ee_pose", "qpos", "gripper_target_rad", "head_degrees"];
// Only present when the base actually drove during the recording.
const BASE_FIELDS = ["base_command", "base_dead_reckoned"];

const fmt = (/** @type {number} */ v, n = 3) => (Number.isFinite(v) ? v.toFixed(n) : "—");
const mb = (/** @type {number} */ b) => `${(b / 1e6).toFixed(0)} MB`;

/**
 * @param {HTMLElement} parent
 * @param {(path: string) => void} onSelect Fires with the chosen .h5 path (or "").
 * @returns {{ el: HTMLElement, path: () => string, destroy: () => void }}
 */
export function createDemoPanel(parent, onSelect) {
  let selected = "";
  let aborter = /** @type {AbortController | null} */ (null);
  let destroyed = false;

  const el = document.createElement("section");
  el.className = "icl-panel icl-demo";
  el.innerHTML = `
    <header class="icl-panel-head">
      <h2>Demonstration</h2>
      <span class="icl-demo-status microlabel"></span>
    </header>
    <div class="icl-demo-pick">
      <select class="icl-select" aria-label="Recorded demonstration"></select>
      <label class="icl-param microlabel">frames
        <input type="number" class="icl-input mono icl-demo-count" min="6" max="48" step="1" value="24" />
      </label>
      <label class="icl-param microlabel">selection
        <select class="icl-select icl-demo-mode">
          <option value="keyframes">keyframes</option>
          <option value="uniform">uniform</option>
        </select>
      </label>
    </div>
    <dl class="icl-demo-facts"></dl>
    <p class="icl-demo-caption microlabel"></p>
    <div class="icl-strip" role="list"></div>
    <div class="icl-frame-detail" hidden></div>`;

  const $ = (/** @type {string} */ sel) => /** @type {HTMLElement} */ (el.querySelector(sel));
  const select = /** @type {HTMLSelectElement} */ (el.querySelector(".icl-select"));
  const count = /** @type {HTMLInputElement} */ (el.querySelector(".icl-demo-count"));
  const mode = /** @type {HTMLSelectElement} */ (el.querySelector(".icl-demo-mode"));
  const status = $(".icl-demo-status");
  const facts = $(".icl-demo-facts");
  const caption = $(".icl-demo-caption");
  const strip = $(".icl-strip");
  const detail = $(".icl-frame-detail");

  /** @param {string} text @param {boolean} [bad] */
  function setStatus(text, bad = false) {
    status.textContent = text;
    status.classList.toggle("is-bad", bad);
  }

  /** @param {Record<string, string>} rows */
  function renderFacts(rows) {
    facts.replaceChildren(
      ...Object.entries(rows).flatMap(([k, v]) => {
        const dt = document.createElement("dt");
        dt.textContent = k;
        const dd = document.createElement("dd");
        dd.textContent = v;
        return [dt, dd];
      }),
    );
  }

  /** @param {any} frame */
  function showFrame(frame) {
    detail.hidden = false;
    const cams = frame.camera_observations ?? {};
    const pose = /** @type {number[]} */ (frame.ee_pose ?? []);
    detail.replaceChildren();

    const shots = document.createElement("div");
    shots.className = "icl-frame-shots";
    for (const name of ["head", "wrist"]) {
      const figure = document.createElement("figure");
      const img = document.createElement("img");
      img.src = `data:image/jpeg;base64,${frame.images?.[name] ?? ""}`;
      img.alt = `${name} camera at source frame ${frame.index}`;
      const cap = document.createElement("figcaption");
      cap.className = "microlabel";
      const obs = cams[name];
      // row_offset_s is how stale this image is relative to the row it is filed
      // under; the battery episode has wrist frames up to 1.8 s behind.
      cap.textContent = obs
        ? `${name} · source t=${fmt(obs.source_time_s, 2)}s · offset ${fmt(obs.row_offset_s, 2)}s`
        : name;
      if (obs && Math.abs(obs.row_offset_s) > 0.25) cap.classList.add("is-stale");
      figure.append(img, cap);
      shots.append(figure);
    }

    const numbers = document.createElement("dl");
    numbers.className = "icl-demo-facts";
    const rows = {
      index: String(frame.index),
      "time_s": `${fmt(frame.time_s, 2)} s`,
      "ee_pose xyz": pose.slice(0, 3).map((v) => fmt(v)).join("  "),
      "ee_pose quat": pose.slice(3).map((v) => fmt(v)).join("  "),
      "gripper_target_rad": fmt(frame.gripper_target_rad),
      "head_degrees": frame.head_degrees == null ? "—" : fmt(frame.head_degrees, 1),
      qpos: (frame.qpos ?? []).map((/** @type {number} */ v) => fmt(v, 2)).join("  "),
    };
    if (frame.base_command) {
      rows["base_command"] = `${fmt(frame.base_command[0], 2)} m/s  ${fmt(frame.base_command[1], 2)} rad/s`;
      rows["base_dead_reckoned"] = (frame.base_dead_reckoned ?? []).map((/** @type {number} */ v) => fmt(v, 2)).join("  ");
    }
    numbers.replaceChildren(
      ...Object.entries(rows).flatMap(([k, v]) => {
        const dt = document.createElement("dt");
        dt.textContent = k;
        const dd = document.createElement("dd");
        dd.className = "mono";
        dd.textContent = v;
        return [dt, dd];
      }),
    );
    detail.append(shots, numbers);
  }

  /** @param {any[]} frames */
  function renderStrip(frames) {
    strip.replaceChildren(
      ...frames.map((frame, i) => {
        const card = document.createElement("button");
        card.type = "button";
        card.className = "icl-frame";
        card.setAttribute("role", "listitem");
        card.title = `Source frame ${frame.index} — the model sees both cameras plus ${CONTEXT_FIELDS.join(", ")}`;
        const img = document.createElement("img");
        img.loading = "lazy";
        img.src = `data:image/jpeg;base64,${frame.images?.head ?? ""}`;
        img.alt = `head camera at source frame ${frame.index}`;
        const tag = document.createElement("span");
        tag.className = "icl-frame-tag mono";
        tag.textContent = `${frame.index} · ${fmt(frame.time_s, 1)}s`;
        card.append(img, tag);
        card.addEventListener("click", () => {
          for (const other of strip.querySelectorAll(".icl-frame")) other.classList.remove("is-open");
          card.classList.add("is-open");
          showFrame(frame);
        });
        if (i === 0) card.classList.add("is-open");
        return card;
      }),
    );
    if (frames.length) showFrame(frames[0]);
  }

  /** @param {string} path */
  async function load(path) {
    aborter?.abort();
    aborter = new AbortController();
    strip.replaceChildren();
    detail.hidden = true;
    renderFacts({});
    caption.textContent = "";
    if (!path) {
      setStatus("none selected");
      return;
    }
    setStatus("reading episode…");
    try {
      const query = `path=${encodeURIComponent(path)}&max=${count.value}&selection=${mode.value}`;
      const res = await fetch(`${DETAIL_URL}?${query}`, { signal: aborter.signal });
      if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
      const demo = await res.json();
      if (destroyed) return;
      renderFacts({
        skill: demo.skill,
        episode: `${demo.episode_frames} recorded rows`,
        "context frames": `${demo.frames.length} at these settings`,
        "gripper events": (demo.grip_events ?? []).length
          ? `${demo.grip_events.length} grasp/release`
          : "none detected",
        span: `${fmt(demo.duration_s, 1)} s`,
        "final ee xyz": (demo.final_pose ?? []).slice(0, 3).map((/** @type {number} */ v) => fmt(v)).join("  "),
        base: demo.base_moved
          ? `drove ${fmt(demo.base_drive_m, 2)} m · turned ${fmt(demo.base_turn_rad, 2)} rad`
          : "stationary",
        "robot model": String(demo.model_sha256 ?? "").slice(0, 12),
      });
      const fields = demo.base_moved ? [...CONTEXT_FIELDS, ...BASE_FIELDS] : CONTEXT_FIELDS;
      caption.textContent =
        `Each frame below goes into the request as both camera images plus ${fields.join(", ")}. ` +
        (demo.base_moved
          ? "The base drove during this recording, so its ee_pose changed partly because the robot " +
            "moved. Those figures are integrated from the recorded commands, not measured odometry. " +
            "A run is arm-only, so the model is told to adapt the approach rather than copy the reach. "
          : "") +
        `A phase-aware run pulls further frames on demand with inspect_demo.`;
      el.classList.toggle("has-base-motion", !!demo.base_moved);
      renderStrip(demo.frames ?? []);
      setStatus(`${demo.frames.length} context frames`);
    } catch (err) {
      if (/** @type {any} */ (err)?.name === "AbortError") return;
      setStatus(`could not read: ${/** @type {any} */ (err)?.message ?? err}`, true);
    }
  }

  select.addEventListener("change", () => {
    selected = select.value;
    onSelect(selected);
    void load(selected);
  });
  // Preview the frame set a run would actually send, not a fixed default.
  for (const control of [count, mode]) control.addEventListener("change", () => void load(selected));

  (async () => {
    try {
      const res = await fetch(LIST_URL);
      if (!res.ok) throw new Error(String(res.status));
      const { demonstrations } = await res.json();
      if (destroyed) return;
      if (!demonstrations.length) {
        setStatus("no recorded episodes with camera frames", true);
        return;
      }
      select.replaceChildren(
        ...demonstrations.map((/** @type {any} */ d) => {
          const option = document.createElement("option");
          option.value = d.path;
          option.textContent = `${d.skill} · ${d.episode} · ${mb(d.size_bytes)}`;
          return option;
        }),
      );
      selected = demonstrations[0].path;
      select.value = selected;
      onSelect(selected);
      void load(selected);
    } catch (err) {
      setStatus(`roster unavailable: ${/** @type {any} */ (err)?.message ?? err}`, true);
    }
  })();

  parent.appendChild(el);
  return {
    el,
    path: () => selected,
    destroy: () => {
      destroyed = true;
      aborter?.abort();
      el.remove();
    },
  };
}
