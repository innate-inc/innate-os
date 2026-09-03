// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// The speed and ramp knobs, on the page where the robot is driving.
//
// They live here and not only in Settings because they are tuned by feel, at
// the moment the robot misbehaves: the node applies a write mid-run, so a value
// can change between one doorway and the next. Saving is separate and explicit
// — a run's worth of experimenting should not rewrite the robot's config, and a
// value worth keeping should not evaporate on the next restart.

import { ros } from "../rosClient.js";
import { NAV_POLICY_NODE } from "../constants.js";

/** rcl_interfaces/msg/ParameterType.PARAMETER_DOUBLE. */
const PARAM_DOUBLE = 3;

/**
 * @typedef {Object} Knob
 * @property {string} name   ROS parameter name on the policy node.
 * @property {string} label
 * @property {string} unit
 * @property {number} step
 */

/** @type {Knob[]} */
const KNOBS = [
  { name: "max_linear_speed", label: "forward", unit: "m/s", step: 0.05 },
  { name: "max_linear_accel", label: "speed up", unit: "m/s²", step: 0.05 },
  { name: "max_linear_decel", label: "brake", unit: "m/s²", step: 0.1 },
  { name: "max_angular_speed", label: "turn", unit: "rad/s", step: 0.1 },
  { name: "max_angular_accel", label: "turn up", unit: "rad/s²", step: 0.1 },
  { name: "max_angular_decel", label: "turn brake", unit: "rad/s²", step: 0.1 },
];

/** @param {number} v */
const trim = (v) => String(Number(v.toFixed(4)));

/** @param {HTMLElement} parent */
export function createTuningPanel(parent) {
  const root = document.createElement("div");
  root.className = "policy-panel policy-tuning";
  root.innerHTML = `
    <button class="policy-panel-head policy-fold" type="button" aria-expanded="false">
      <span class="policy-fold-mark">\u203a</span>Tuning
    </button>
    <div class="policy-knobs" hidden>
      ${KNOBS.map((k) => `
        <div class="policy-knob" data-k="${k.name}">
          <label for="knob-${k.name}">${k.label}</label>
          <input id="knob-${k.name}" type="number" step="${k.step}" min="0" disabled />
          <span>${k.unit}</span>
        </div>`).join("")}
    </div>
    <div class="policy-tuning-foot" hidden>
      <button class="policy-tuning-save" type="button" disabled>Save</button>
      <span class="policy-tuning-status">reading…</span>
    </div>`;
  parent.appendChild(root);

  // Collapsed by default: nine knobs is a lot of column for something touched
  // occasionally, and the benchmark below it is what the page is usually for.
  const fold = /** @type {HTMLButtonElement} */ (root.querySelector(".policy-fold"));
  const folded = [root.querySelector(".policy-knobs"), root.querySelector(".policy-tuning-foot")];
  const setOpen = (open) => {
    fold.setAttribute("aria-expanded", String(open));
    /** @type {HTMLElement} */ (fold.querySelector(".policy-fold-mark")).textContent =
      open ? "\u02c5" : "\u203a";
    for (const el of folded) if (el) /** @type {HTMLElement} */ (el).hidden = !open;
    localStorage.setItem("policy-tuning-open", String(open));
  };
  fold.addEventListener("click", () => setOpen(fold.getAttribute("aria-expanded") !== "true"));
  setOpen(localStorage.getItem("policy-tuning-open") === "true");

  const save = /** @type {HTMLButtonElement} */ (root.querySelector(".policy-tuning-save"));
  const status = /** @type {HTMLElement} */ (root.querySelector(".policy-tuning-status"));
  /** @type {Map<string, HTMLInputElement>} */
  const fields = new Map();
  /** The last value the ROBOT confirmed, so a rejected edit has somewhere to go back to. */
  const onRobot = new Map();
  let alive = true;

  /** @param {string} text @param {"ok"|"bad"} [tone] */
  function say(text, tone) {
    status.textContent = text;
    status.classList.toggle("ok", tone === "ok");
    status.classList.toggle("bad", tone === "bad");
  }

  for (const knob of KNOBS) {
    const input = /** @type {HTMLInputElement} */ (
      root.querySelector(`.policy-knob[data-k="${knob.name}"] input`));
    fields.set(knob.name, input);
    // "change", not "input": a number field fires per keystroke, and pushing
    // 0.0 while someone types "0.25" would be rejected by the node as a zero
    // acceleration budget and stamp the field back over what they were typing.
    input.addEventListener("change", () => push(knob, input));
  }

  /** @param {Knob} knob @param {HTMLInputElement} input */
  async function push(knob, input) {
    const value = Number(input.value);
    if (!Number.isFinite(value)) return revert(knob.name);
    try {
      const res = await ros.callService(`${NAV_POLICY_NODE}/set_parameters`, {
        parameters: [{
          name: knob.name,
          value: {
            type: PARAM_DOUBLE, bool_value: false, integer_value: 0,
            double_value: value, string_value: "",
          },
        }],
      });
      // The service resolves even when the node refuses the value, so the
      // result is the only place the refusal appears.
      const result = res?.results?.[0];
      if (result?.successful === false) {
        say(result.reason || `${knob.label} rejected`, "bad");
        return revert(knob.name);
      }
      onRobot.set(knob.name, value);
      save.disabled = false;
      say(`${knob.label} → ${trim(value)} ${knob.unit}`, "ok");
    } catch (err) {
      say(err instanceof Error ? err.message : "the robot did not answer", "bad");
      revert(knob.name);
    }
  }

  /** @param {string} name */
  function revert(name) {
    const previous = onRobot.get(name);
    const input = fields.get(name);
    if (input && previous !== undefined) input.value = trim(previous);
  }

  async function read() {
    try {
      const res = await ros.callService(`${NAV_POLICY_NODE}/get_parameters`,
        { names: KNOBS.map((k) => k.name) });
      const values = res?.values || [];
      if (!alive) return;
      KNOBS.forEach((knob, i) => {
        const value = values[i]?.double_value;
        const input = fields.get(knob.name);
        if (!input || typeof value !== "number") return;
        input.value = trim(value);
        input.disabled = false;
        onRobot.set(knob.name, value);
      });
      say(onRobot.size ? "live — changes apply immediately" : "the policy node is not running",
        onRobot.size ? undefined : "bad");
    } catch {
      if (alive) say("the policy node is not running", "bad");
    }
  }

  // Persisting is what survives a restart: the live write above reaches only
  // the process that is running now.
  save.addEventListener("click", async () => {
    save.disabled = true;
    say("saving…");
    const sets = [...onRobot].map(([name, value]) => ({
      path: ["innate_nav_node", "ros__parameters", name],
      value,
      type: "float",
    }));
    try {
      const res = await fetch("/settings.json", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        cache: "no-store",
        body: JSON.stringify({ sets, clears: [] }),
      });
      const body = await res.json();
      if (body?.ok) say("saved — these are the values it starts with now", "ok");
      else { say(`save failed: ${body?.message || "unknown error"}`, "bad"); save.disabled = false; }
    } catch (err) {
      say(err instanceof Error ? err.message : "save failed", "bad");
      save.disabled = false;
    }
  });

  if (ros.state === "connected") read();
  const unlisten = ros.onStateChange((/** @type {string} */ state) => {
    if (state === "connected") read();
  });

  return {
    destroy() {
      alive = false;
      unlisten();
      root.remove();
    },
  };
}
