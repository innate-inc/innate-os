// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Start/stop for one demonstration-conditioned skill.
//
// Runs go through /execute_skill like every other skill launcher, so a run
// started here is the same run the agent or the CLI would start — and Stop
// falls back to /brain/cancel_skill when this tab doesn't own the goal handle
// (a run started elsewhere, or one that outlived a reload).

import {
  AVAILABLE_SKILLS_TOPIC,
  CANCEL_SKILL_SERVICE,
  EXECUTE_SKILL_ACTION,
  EXECUTE_SKILL_ACTION_TYPE,
  ICL_SKILL_NAMES,
  SKILL_STATUS_UPDATE_TOPIC,
} from "../constants.js";

const leaf = (/** @type {string} */ id) => String(id).split("/").pop() ?? "";

/** A skill the server listed but could not import. */
const broken = (/** @type {any} */ s) => s?.type === "broken" || !!s?.load_error;

/** @param {any} skill */
function inputSchema(skill) {
  if (skill?.inputs && typeof skill.inputs === "object" && !Array.isArray(skill.inputs)) return skill.inputs;
  if (typeof skill?.inputs_json === "string" && skill.inputs_json) {
    try {
      return JSON.parse(skill.inputs_json);
    } catch {
      return {};
    }
  }
  return {};
}

/**
 * @param {HTMLElement} parent
 * @param {import("../rosClient.js").RosClient} ros
 * @param {{ demonstration: () => string, onFeedback: (text: string) => void }} opts
 * @returns {{ el: HTMLElement, destroy: () => void }}
 */
export function createRunControls(parent, ros, opts) {
  /** @type {any[]} */ let roster = [];
  // Distinguishes "no roster message yet" from "a roster with no ICL skills".
  let seenRoster = false;
  /** @type {{ cancel: () => void } | null} */ let owned = null;
  let activeName = "";
  let stopping = false;
  /** @type {Record<string, string>} */ const overrides = {};

  const el = document.createElement("footer");
  el.className = "icl-run";
  el.innerHTML = `
    <select class="icl-select icl-run-skill" aria-label="Skill"></select>
    <div class="icl-run-params"></div>
    <span class="icl-run-msg microlabel"></span>
    <button type="button" class="icl-btn icl-start">Start</button>
    <button type="button" class="icl-btn icl-stop" disabled>Stop</button>`;

  const $ = (/** @type {string} */ s) => /** @type {HTMLElement} */ (el.querySelector(s));
  const picker = /** @type {HTMLSelectElement} */ (el.querySelector(".icl-run-skill"));
  const params = $(".icl-run-params");
  const message = $(".icl-run-msg");
  const start = /** @type {HTMLButtonElement} */ (el.querySelector(".icl-start"));
  const stop = /** @type {HTMLButtonElement} */ (el.querySelector(".icl-stop"));

  const current = () => roster.find((s) => s.id === picker.value);

  /** @param {string} text @param {boolean} [bad] */
  function say(text, bad = false) {
    message.textContent = text;
    message.classList.toggle("is-bad", bad);
  }

  /** Build the control a param's declared type deserves: a checkbox for a bool,
   *  a dropdown for a Literal's enum, a text box otherwise. The .h5 comes from
   *  the picker, so it is never rendered. */
  function renderParams() {
    const schema = inputSchema(current());
    const editable = Object.entries(schema).filter(([name]) => name !== "demonstration");
    params.replaceChildren(...editable.map(([name, spec]) => paramControl(name, spec)));
  }

  /** @param {string} name @param {any} spec */
  function paramControl(name, spec) {
    const label = document.createElement("label");
    label.className = "icl-param microlabel";
    const type = String(spec?.type ?? "str");
    const stored = overrides[name];

    if (type === "bool") {
      const box = document.createElement("input");
      box.type = "checkbox";
      box.className = "icl-toggle";
      box.checked = stored === undefined ? !!spec?.default : stored === "true";
      box.addEventListener("change", () => {
        overrides[name] = String(box.checked);
      });
      label.classList.add("is-toggle");
      label.append(box, name);
      return label;
    }

    label.textContent = name;
    if (Array.isArray(spec?.enum) && spec.enum.length) {
      const menu = document.createElement("select");
      menu.className = "icl-select icl-param-select";
      menu.replaceChildren(
        ...spec.enum.map((/** @type {any} */ value) => {
          const option = document.createElement("option");
          option.value = String(value);
          option.textContent = String(value);
          return option;
        }),
      );
      menu.value = stored ?? String(spec?.default ?? spec.enum[0]);
      menu.addEventListener("change", () => {
        overrides[name] = menu.value;
      });
      label.appendChild(menu);
      return label;
    }

    const input = document.createElement("input");
    input.type = "text";
    input.className = "icl-input mono";
    input.value = stored ?? String(spec?.default ?? "");
    input.size = 8;
    input.addEventListener("input", () => {
      overrides[name] = input.value;
    });
    label.appendChild(input);
    return label;
  }

  /** @returns {Record<string, any> | { error: string }} */
  function buildInputs() {
    const schema = inputSchema(current());
    /** @type {Record<string, any>} */ const inputs = {};
    for (const [name, spec] of Object.entries(schema)) {
      const type = String(/** @type {any} */ (spec)?.type ?? "str");
      const raw = name === "demonstration" ? opts.demonstration() : (overrides[name] ?? "");
      const fallback = /** @type {any} */ (spec)?.default;
      if (type === "bool") {
        inputs[name] = raw === "" ? !!fallback : raw === "true" || raw === "1";
        continue;
      }
      if (!raw) {
        if (fallback !== undefined) continue; // let the skill's own default stand
        if (/** @type {any} */ (spec)?.required) return { error: `${name} is required` };
        continue;
      }
      if (type === "int" || type === "float") {
        const n = Number(raw);
        if (!Number.isFinite(n)) return { error: `${name} must be a number` };
        inputs[name] = type === "int" ? Math.trunc(n) : n;
      } else if (type === "bool") {
        inputs[name] = raw === "true" || raw === "1";
      } else {
        inputs[name] = raw;
      }
    }
    return inputs;
  }

  function syncButtons() {
    const connected = ros.state === "connected";
    const running = !!owned || !!activeName;
    start.disabled = !connected || running || !current();
    stop.disabled = !connected || !running || stopping;
    stop.textContent = stopping ? "Stopping" : "Stop";
    el.classList.toggle("is-running", running);
  }

  function launch() {
    const skill = current();
    if (!skill) return;
    const built = buildInputs();
    if ("error" in built) {
      say(built.error, true);
      return;
    }
    say(`starting ${leaf(skill.id)}…`);
    const { promise, cancel } = ros.sendActionGoal(
      EXECUTE_SKILL_ACTION,
      EXECUTE_SKILL_ACTION_TYPE,
      { skill_type: skill.id, inputs: JSON.stringify(built) },
      {
        onFeedback: (/** @type {any} */ fb) => {
          const text = fb?.feedback ?? fb?.values?.feedback;
          if (text) opts.onFeedback(String(text));
        },
      },
    );
    owned = { cancel };
    syncButtons();
    promise.then(
      (/** @type {any} */ result) => {
        const values = result?.values ?? result;
        owned = null;
        stopping = false;
        say(values?.message || (values?.success ? "Done" : "Finished"), !values?.success);
        syncButtons();
      },
      (/** @type {any} */ err) => {
        owned = null;
        stopping = false;
        say(err?.message ?? "Run failed", true);
        syncButtons();
      },
    );
  }

  function halt() {
    if (stopping) return;
    stopping = true;
    syncButtons();
    say("stopping…");
    if (owned) {
      owned.cancel();
      return;
    }
    // No goal handle here — ask the skills server to cancel whatever is running.
    ros.callService(CANCEL_SKILL_SERVICE).then(
      () => say("stop requested"),
      (/** @type {any} */ err) => {
        stopping = false;
        say(err?.message ?? "Could not stop", true);
        syncButtons();
      },
    );
  }

  start.addEventListener("click", launch);
  stop.addEventListener("click", halt);
  picker.addEventListener("change", () => {
    renderParams();
    syncButtons();
  });

  const unsubRoster = ros.subscribe(
    AVAILABLE_SKILLS_TOPIC,
    (msg) => {
      // brain_messages/AvailableSkills carries the array on the message itself;
      // it is not a JSON string like the other /brain topics.
      const all = Array.isArray(msg?.skills) ? msg.skills : [];
      const next = all.filter((/** @type {any} */ s) => s?.id && ICL_SKILL_NAMES.includes(leaf(s.id)));
      const same =
        seenRoster &&
        next.length === roster.length &&
        next.every((/** @type {any} */ s, /** @type {number} */ i) => s.id === roster[i]?.id && s.type === roster[i]?.type);
      if (same) return;
      seenRoster = true;
      roster = next;
      const keep = picker.value;
      picker.replaceChildren(
        ...roster.map((s) => {
          const option = document.createElement("option");
          option.value = s.id;
          // A skill the server could not import is listed but not launchable.
          option.textContent = broken(s) ? `${leaf(s.id)} (failed to load)` : leaf(s.id);
          option.disabled = broken(s);
          if (s.load_error) option.title = String(s.load_error);
          return option;
        }),
      );
      const launchable = roster.find((s) => !broken(s));
      picker.value = roster.some((s) => s.id === keep && !broken(s)) ? keep : (launchable?.id ?? "");
      if (!roster.length) say("no demonstration skills in the roster", true);
      else if (!launchable) say(roster[0].load_error ?? "every demonstration skill failed to load", true);
      else say("");
      renderParams();
      syncButtons();
    },
    undefined,
    "brain_messages/msg/AvailableSkills",
  );

  // A run started elsewhere still drives the buttons, so Stop is never dead.
  const unsubStatus = ros.subscribe(
    SKILL_STATUS_UPDATE_TOPIC,
    (msg) => {
      const raw = msg?.data ?? msg?.msg?.data;
      /** @type {any} */ let payload;
      try {
        payload = typeof raw === "string" ? JSON.parse(raw) : msg;
      } catch {
        return;
      }
      const name = payload?.primitive_name ?? payload?.skill_name ?? "";
      if (!name) return;
      const wasRunning = !!activeName;
      activeName = payload?.status === "running" ? String(name) : "";
      if (wasRunning && !activeName) stopping = false;
      syncButtons();
    },
    undefined,
    "std_msgs/msg/String",
  );

  const unsubState = ros.onStateChange(syncButtons);
  say("waiting for the skill roster…");
  syncButtons();
  parent.appendChild(el);

  return {
    el,
    destroy: () => {
      unsubRoster();
      unsubStatus();
      unsubState();
      el.remove();
    },
  };
}
