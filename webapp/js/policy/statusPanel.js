// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// The inference readout: the three clocks, and what the run is doing.
//
// They run independently (streaming ~5Hz, inference free-running, control
// 50Hz), and most confusing behaviour is one of them stalling while the others
// carry on. So the panel reports them separately rather than as a single
// "connected" light. What the model is looking at is the bar's job (obsBar.js).

const FIELDS = [
  ["episode", (s) => s.episode_id || "—"],
  ["model", (s) => s.plan?.model || "—"],
  ["server", (s) => s.server || "—"],
];

/** @param {number} v @param {number} [dp] */
const secs = (v, dp = 2) => (typeof v === "number" ? `${v.toFixed(dp)}s` : "—");

export function createStatusPanel(parent) {
  const root = document.createElement("div");
  root.className = "policy-panel";
  root.innerHTML = `
    <div class="policy-panel-head">Inference</div>
    <div class="policy-meta"></div>
    <div class="policy-clocks">
      <div class="policy-clock" data-k="frames"><b>—</b><span>observation sent</span></div>
      <div class="policy-clock" data-k="plans"><b>—</b><span>plan rate</span></div>
      <div class="policy-clock" data-k="age"><b>—</b><span>plan age</span></div>
      <div class="policy-clock" data-k="compute"><b>—</b><span>compute</span></div>
    </div>
    <div class="policy-panel-head">State</div>
    <div class="policy-flags"></div>`;
  parent.appendChild(root);

  const meta = root.querySelector(".policy-meta");
  const flags = root.querySelector(".policy-flags");
  const clock = (k) => root.querySelector(`.policy-clock[data-k="${k}"] b`);

  /** @param {any} s the parsed /nav_policy/status payload */
  function setStatus(s) {
    if (meta) {
      meta.innerHTML = FIELDS.map(([k, get]) =>
        `<div><span>${k}</span><code>${get(s)}</code></div>`).join("");
    }
    const plan = s.plan;
    const el = (k, v) => { const n = clock(k); if (n) n.textContent = v; };
    el("frames", secs(s.last_frame_age_s));
    el("plans", typeof s.plan_rate_hz === "number" && s.plan_rate_hz > 0
      ? `${s.plan_rate_hz.toFixed(1)} Hz` : "—");
    el("age", plan ? secs(plan.age_s) : "—");
    el("compute", plan ? `${Math.round(plan.compute_ms)} ms` : "—");

    if (flags) {
      const on = [];
      if (s.running) on.push("running");
      if (s.arrived) on.push("arrived");
      if (plan?.stop) on.push("stop");
      if (s.blocked_recovery) on.push("blocked recovery");
      on.push(s.costmap ? "costmap" : "no costmap");
      flags.innerHTML = on.map((t) => `<span class="policy-flag">${t}</span>`).join("");
    }
  }

  function clear() {
    flags && (flags.innerHTML = "");
  }

  return { setStatus, clear, destroy: () => root.remove() };
}
