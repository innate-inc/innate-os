// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Brain monitor — a live window into the local Gemini agent loop. Not a page:
// the Agent page embeds it as its deep view (createBrainMonitor), flipped in
// over the camera stage while the agent panel stays docked alongside.
//
// Deep telemetry rides /brain/trace (JSON on std_msgs/String, published by
// brain_client's BrainAgent): turn lifecycle with every image the model was
// sent (head camera, wrist camera, event images), the full input text and
// system instruction, tool calls with args + outcomes, think latencies, the
// event queue, and a 1 Hz snapshot heartbeat. Recent turns are kept so any
// turn row can be opened in the inspector overlay — the complete model input
// and output for that turn. Everything else (skill runs, pose, live camera
// fallback) comes from the public topics, so the monitor degrades gracefully
// on robots without the trace publisher — the deep panels just stay quiet.
// The mind stream lives in the agent panel next door, not here.
//
// ?demo drives the whole monitor from a scripted synthetic brain (demo.js) —
// useful for UI work with no robot.

import { ros } from "../rosClient.js";
import { isTypingContext } from "../shell.js";

const CAM_TOPIC = "/mars/main_camera/left/image_raw/compressed";
const HIST_MAX = 60; // brain_client default history_max_entries

/** @type {Record<string, string>} */
const PHASE_LABEL = {
  off: "OFFLINE",
  idle: "STANDBY",
  look: "LOOK",
  think: "THINK",
  act: "ACT",
  wait: "NEXT LOOK",
  error: "RETRY IN",
};
/** @type {Record<string, string>} */
const PHASE_INK = { look: "var(--br-aqua)", think: "var(--br-violet)", act: "var(--br-orange)", error: "var(--br-critical)" };
const NODE_ANGLE = { look: -90, think: 30, act: 150 };
const RING_R = 112;
const RING_C = 2 * Math.PI * RING_R;

/**
 * Build the monitor into `root` (the Agent page's brain layer) and start its
 * feeds. `onRequestClose` is the monitor asking to flip back to the live view
 * (Escape with no inspector open). While hidden (`setVisible(false)`) the trace
 * feeds stay live so turn history keeps accumulating, but the animation loop
 * and the wire-heavy live-camera fallback pause.
 * @param {HTMLElement} root
 * @param {{ onRequestClose?: () => void }} [opts]
 * @returns {{ destroy: () => void, setVisible: (visible: boolean) => void }}
 */
export function createBrainMonitor(root, opts = {}) {
  root.innerHTML = template();
  /** @param {string} sel */
  const $ = (sel) => /** @type {HTMLElement} */ (root.querySelector(sel));

  // ---- live state ----------------------------------------------------------
  const S = {
    phase: "off",
    phaseAt: performance.now(),
    turn: 0,
    waitUntil: 0,
    waitTotal: 0,
    streak: 0,
    /** @type {{turn: number, s: number}[]} */
    latencies: [],
    history: 0,
    uptime: 0,
    uptimeAt: 0,
    /** @type {string | null} */
    running: null,
    haveTrace: false,
    cometAngle: -90,
  };

  // Per-turn records for the filmstrip + inspector: turn -> {start, end, request}.
  // Frames are full base64 JPEGs, so the store is deliberately shallow.
  /** @type {Map<number, {start: any, end: any, request: any}>} */
  const turns = new Map();
  const TURNS_MAX = 24;
  let shownTurn = 0; // turn on the vision stage
  let inspecting = 0; // turn open in the inspector; 0 = closed
  let visible = true; // built on first open, so the monitor starts on screen

  // One-shot UI timers (phase eases, queue drain), all cleared on destroy so
  // none fires against the wiped DOM after the page unmounts.
  /** @type {Set<number>} */
  const uiTimers = new Set();
  /** @param {() => void} fn @param {number} ms */
  function later(fn, ms) {
    const id = window.setTimeout(() => {
      uiTimers.delete(id);
      fn();
    }, ms);
    uiTimers.add(id);
  }

  /** @param {any} d turn_start payload (frames already normalized) */
  function rememberTurn(d) {
    turns.set(d.turn, { start: d, end: null, request: null });
    while (turns.size > TURNS_MAX) turns.delete(/** @type {number} */ (turns.keys().next().value));
    /** @type {HTMLButtonElement} */ ($(".br-inspect-btn")).hidden = false;
  }

  // ---- the loop ------------------------------------------------------------
  for (const [n, a] of Object.entries(NODE_ANGLE)) {
    const rad = (a * Math.PI) / 180;
    $(`.br-node.${n}`).setAttribute(
      "transform",
      `translate(${150 + RING_R * Math.cos(rad)}, ${150 + RING_R * Math.sin(rad)})`,
    );
  }
  $(".br-ring-count").style.strokeDasharray = String(RING_C);

  /** @param {string} p */
  function setPhase(p) {
    if (S.phase === p) return;
    S.phase = p;
    S.phaseAt = performance.now();
    for (const n of ["look", "think", "act"]) $(`.br-node.${n}`).classList.toggle("active", p === n);
    $(".br-comet").style.opacity = p === "think" ? "1" : "0";
    for (const t of root.querySelectorAll(".br-trail")) /** @type {HTMLElement} */ (t).style.opacity = p === "think" ? "0.45" : "0";
    $(".br-ring-count").style.stroke = p === "error" ? "var(--br-critical)" : p === "wait" ? "var(--br-aqua)" : "transparent";
    $(".br-phase").textContent = PHASE_LABEL[p] ?? p;
    $(".br-phase").style.fill = PHASE_INK[p] || "var(--muted)";
  }

  // ---- handlers (fed by ros subscriptions or the demo driver) --------------
  /** @param {any} d */
  function onTrace(d) {
    S.haveTrace = true;
    if (d.ev === "turn_start") {
      S.turn = d.turn;
      $(".br-tools").textContent = d.tools ? `${d.tools.length} tool${d.tools.length === 1 ? "" : "s"} armed` : "";
      // Older robots trace a single `frame`; current ones a labeled `frames` list.
      d.frames = d.frames || (d.frame ? [{ label: "head camera", jpeg: d.frame }] : []);
      if (d.frames.length) {
        rememberTurn(d);
        showTurn(d.turn, 0);
        sweep();
      }
      hidePing();
      drainQueue();
      setPhase("look");
      later(() => {
        if (S.phase === "look") setPhase("think");
      }, 550);
    }
    if (d.ev === "turn_request") {
      // The exact request body, intercepted just before the wire.
      const rec = turns.get(d.turn);
      if (rec) {
        rec.request = d.body;
        if (inspecting === d.turn) renderInspector();
      }
    }
    if (d.ev === "turn_end") {
      S.streak = 0;
      renderStreak();
      S.history = d.history;
      S.latencies.push({ turn: d.turn, s: d.latency });
      if (S.latencies.length > 60) S.latencies.shift();
      const rec = turns.get(d.turn);
      if (rec) rec.end = d;
      if (inspecting === d.turn) renderInspector();
      addTurnRow(d);
      renderVitals();
      const point = (d.calls || []).find((/** @type {any} */ c) => c.name === "go_to_point_in_view" || c.name === "go_to_point");
      if (point?.args) showPing(point.args.x, point.args.y);
      const acted = (d.calls || []).some((/** @type {any} */ c) => c.name !== "wait") || d.speech;
      setPhase(acted ? "act" : "wait");
      S.waitUntil = performance.now() + d.next_in * 1000;
      S.waitTotal = d.next_in * 1000;
      if (acted)
        later(() => {
          if (S.phase === "act") setPhase("wait");
        }, 1100);
    }
    if (d.ev === "turn_error") {
      S.streak = d.streak;
      renderStreak();
      setPhase("error");
      S.waitUntil = performance.now() + d.backoff * 1000;
      S.waitTotal = d.backoff * 1000;
      const sub = $(".br-loop-sub");
      sub.textContent = trunc(d.error, 90);
      sub.classList.add("err");
    }
    if (d.ev === "event") {
      addQueueCard(d.kind, d.text);
      if (d.kind === "motion") motionCue();
    }
    if (d.ev === "snapshot") onSnapshot(d);
  }

  /** @param {any} d */
  function onSnapshot(d) {
    S.history = d.history;
    S.streak = d.streak;
    S.uptime = d.uptime;
    S.uptimeAt = performance.now();
    S.running = d.running;
    $(".br-chip-backend b").textContent = d.backend || "—";
    $(".br-chip-model b").textContent = d.model || "—";
    renderQueue(d.queued || []);
    renderStreak();
    renderVitals();
    if (!d.active) {
      setPhase("off");
      return;
    }
    if (d.in_flight) {
      if (S.phase !== "think" && S.phase !== "look") setPhase("think");
    } else if (S.phase === "off" || S.phase === "idle") {
      setPhase("wait");
      S.waitUntil = performance.now() + d.next_in * 1000;
      S.waitTotal = Math.max(d.next_in * 1000, 1);
    }
  }

  /** @param {any} d */
  function onAgentStatus(d) {
    $(".br-chip-active b").textContent = d.brain_active ? "active" : "stopped";
    $(".br-chip-active").className = "br-chip br-chip-active " + (d.brain_active ? "live" : "off");
    $(".br-chip-directive b").textContent = d.current_directive || "—";
    if (!d.brain_active) setPhase("off");
    else if (S.phase === "off") setPhase("idle");
  }

  /** @param {any} d */
  function onBrainStatus(d) {
    if (!S.haveTrace) $(".br-chip-backend b").textContent = d.backend || "—";
  }

  /** @param {number} x @param {number} y @param {number} yaw */
  function setPose(x, y, yaw) {
    $(".br-pose-txt").textContent = `x ${x.toFixed(2)}  y ${y.toFixed(2)}  θ ${((yaw * 180) / Math.PI).toFixed(0)}°`;
    $(".br-needle").style.transform = `rotate(${(-yaw * 180) / Math.PI}deg)`;
  }

  /** @param {boolean} on */
  function setSpeaking(on) {
    $(".br-chip-voice").classList.toggle("talk", on);
  }

  // ---- renderers -----------------------------------------------------------
  /** @param {string | undefined} s @param {number} n */
  const trunc = (s, n) => (s && s.length > n ? s.slice(0, n - 1) + "…" : s || "");
  // Model- and skill-supplied strings go through here before any innerHTML —
  // tool names, args, and failure reasons are attacker-influenceable text.
  /** @param {unknown} s */
  const esc = (s) =>
    String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");

  /** @param {string} panelSel @param {HTMLElement} el @param {number} [cap] */
  function prepend(panelSel, el, cap = 60) {
    const feed = $(panelSel);
    feed.querySelector(".br-empty")?.remove();
    feed.prepend(el);
    while (feed.children.length > cap) /** @type {Element} */ (feed.lastChild).remove();
  }

  /** @param {any} d */
  function addTurnRow(d) {
    const div = document.createElement("div");
    div.className = "br-turn-row" + (turns.has(d.turn) ? " click" : "");
    if (turns.has(d.turn)) {
      div.title = "Show the full model input for this turn";
      div.addEventListener("click", () => openInspector(d.turn));
    }
    const calls = (d.calls || [])
      .map((/** @type {any} */ c) => {
        const bad = /^(rejected|unknown|could not)/.test(c.outcome || "");
        const quiet = c.name === "wait";
        const args =
          c.args && Object.keys(c.args).length
            ? "(" +
              Object.entries(c.args)
                .map(([k, v]) => `${k}:${JSON.stringify(v)}`)
                .join(" ") +
              ")"
            : "";
        const title = esc(c.outcome || `${c.name} — no outcome reported`);
        return `<span class="br-call ${bad ? "bad" : quiet ? "quiet" : "go"}" title="${title}">${esc(c.name)}${esc(trunc(args, 46))}</span>`;
      })
      .join("");
    div.innerHTML =
      `<div class="head"><b>turn ${d.turn}</b><span>${(d.calls || []).length ? "" : "observed"}</span>` +
      `<span class="lat" title="Model inference latency for this turn">${d.latency.toFixed(1)}s think</span></div>` +
      (calls ? `<div class="calls">${calls}</div>` : "");
    prepend(".br-actions", div);
  }

  /** @type {Map<string, HTMLElement>} */
  const skillRows = new Map();
  /** @param {any} d */
  function onSkill(d) {
    const key = d.primitive_id || d.skill_name;
    let row = skillRows.get(key);
    if (row && !row.isConnected) {
      // Evicted from the feed by prepend's cap: writing into the detached
      // node would swallow this update, so start a fresh row.
      skillRows.delete(key);
      row = undefined;
    }
    if (!row) {
      row = document.createElement("div");
      skillRows.set(key, row);
      prepend(".br-actions", row);
      if (skillRows.size > 40) skillRows.delete(/** @type {string} */ (skillRows.keys().next().value));
    }
    row.className = "br-skill-row " + d.status;
    row.title = "/brain/skill_status_update";
    row.innerHTML =
      `<span class="st"></span><span>${esc(d.skill_name || d.primitive_name)}</span>` +
      (d.reason ? `<span class="reason">${esc(trunc(d.reason, 60))}</span>` : "") +
      `<span class="status">${esc(d.status)}</span>`;
  }

  /** @param {string} kind @param {string} text */
  function addQueueCard(kind, text) {
    const div = document.createElement("div");
    div.className = "br-qcard " + (kind === "user" ? "user" : "");
    div.title = "Queued for the next look — the model sees it next turn";
    div.innerHTML = `<div class="k">${kind}</div><div class="t"></div>`;
    /** @type {HTMLElement} */ (div.querySelector(".t")).textContent = trunc(text, 160);
    prepend(".br-queue", div, 20);
  }

  /** @param {{kind: string, text: string}[]} queued */
  function renderQueue(queued) {
    const feed = $(".br-queue");
    feed.innerHTML = "";
    if (!queued.length) {
      feed.innerHTML = `<div class="br-empty">quiet — heartbeat looks only</div>`;
      return;
    }
    queued.forEach((e) => addQueueCard(e.kind, e.text));
  }

  function drainQueue() {
    for (const c of root.querySelectorAll(".br-qcard")) c.classList.add("drain");
    later(() => renderQueue([]), 420);
  }

  function renderStreak() {
    $(".br-streak").innerHTML = "<i></i>".repeat(Math.min(S.streak, 8));
  }

  function renderVitals() {
    $(".br-tile-turns").textContent = String(S.turn);
    const L = S.latencies;
    $(".br-tile-last").textContent = L.length ? L[L.length - 1].s.toFixed(1) : "—";
    $(".br-tile-avg").textContent = L.length ? (L.reduce((a, b) => a + b.s, 0) / L.length).toFixed(1) : "—";
    $(".br-hist-fill").style.width = Math.min(100, (S.history / HIST_MAX) * 100) + "%";
    $(".br-hist-txt").textContent = `${S.history} / ${HIST_MAX}`;
    drawSpark();
  }

  // Single-series sparkline (the panel title names it); hover for values.
  /** @type {{x: number, y: number, turn: number, s: number}[]} */
  let sparkPts = [];
  function drawSpark() {
    const svg = $(".br-spark");
    const L = S.latencies.slice(-40);
    if (L.length < 2) {
      svg.innerHTML = "";
      sparkPts = [];
      return;
    }
    const W = 400, H = 88, PAD = 6;
    const max = Math.max(...L.map((p) => p.s), 1);
    sparkPts = L.map((p, i) => ({
      x: PAD + (i * (W - 2 * PAD)) / (L.length - 1),
      y: H - PAD - (p.s / max) * (H - 2 * PAD),
      ...p,
    }));
    const line = sparkPts.map((p) => `${p.x},${p.y}`).join(" ");
    svg.innerHTML =
      `<polygon class="area" points="${PAD},${H - PAD} ${line} ${W - PAD},${H - PAD}"/>` +
      `<polyline class="line" points="${line}"/>` +
      `<circle class="dot" r="4" style="display:none"/>`;
  }

  $(".br-spark-wrap").addEventListener("mousemove", (e) => {
    if (!sparkPts.length) return;
    const rect = $(".br-spark").getBoundingClientRect();
    const x = ((e.clientX - rect.left) / rect.width) * 400;
    const p = sparkPts.reduce((a, b) => (Math.abs(b.x - x) < Math.abs(a.x - x) ? b : a));
    const dot = $(".br-spark .dot");
    if (dot) {
      dot.style.display = "";
      dot.setAttribute("cx", String(p.x));
      dot.setAttribute("cy", String(p.y));
    }
    const tip = $(".br-spark-tip");
    tip.style.display = "block";
    tip.style.left = (p.x / 400) * rect.width + "px";
    tip.style.top = (p.y / 88) * rect.height + "px";
    tip.textContent = `turn ${p.turn} · ${p.s.toFixed(2)}s`;
  });
  $(".br-spark-wrap").addEventListener("mouseleave", () => {
    $(".br-spark-tip").style.display = "none";
    const dot = $(".br-spark .dot");
    if (dot) dot.style.display = "none";
  });

  // ---- vision --------------------------------------------------------------
  /** Put one of a turn's frames on the stage and sync the filmstrip.
   * @param {number} turn @param {number} idx */
  function showTurn(turn, idx) {
    const rec = turns.get(turn);
    const f = rec?.start.frames[idx];
    if (!f) return;
    shownTurn = turn;
    showFrame("data:image/jpeg;base64," + f.jpeg, `what the brain saw · turn ${turn} · ${f.label}`);
    if (idx !== 0) hidePing(); // the go-to-point ping is grounded in the head frame
    const film = $(".br-film");
    film.innerHTML = "";
    film.classList.toggle("multi", rec.start.frames.length > 1);
    rec.start.frames.forEach((/** @type {any} */ fr, /** @type {number} */ i) => {
      const b = document.createElement("button");
      b.className = "br-thumb" + (i === idx ? " active" : "");
      b.innerHTML = `<img alt=""><span></span>`;
      /** @type {HTMLImageElement} */ (b.querySelector("img")).src = "data:image/jpeg;base64," + fr.jpeg;
      /** @type {HTMLElement} */ (b.querySelector("span")).textContent = fr.label;
      b.title = `${fr.label} — click to inspect`;
      b.addEventListener("click", () => showTurn(turn, i));
      film.append(b);
    });
  }

  // ---- turn inspector ------------------------------------------------------
  /** @param {number} turn */
  function openInspector(turn) {
    if (!turns.has(turn)) return;
    inspecting = turn;
    renderInspector();
    $(".br-inspect").hidden = false;
  }
  function closeInspector() {
    inspecting = 0;
    $(".br-inspect").hidden = true;
  }

  /** A labeled block for the inspector's output section.
   * @param {string} label @param {string} text @param {string} cls */
  function iBlock(label, text, cls) {
    const div = document.createElement("div");
    div.className = "br-i-block " + cls;
    div.innerHTML = `<div class="l"></div><div class="t"></div>`;
    /** @type {HTMLElement} */ (div.querySelector(".l")).textContent = label;
    /** @type {HTMLElement} */ (div.querySelector(".t")).textContent = text;
    return div;
  }

  /** One request `contents` entry as a card: role tag + every part rendered —
   * text as prose, inlineData as the actual image, function calls/responses
   * as mono lines. Nothing summarized, nothing dropped.
   * @param {any} content @param {boolean} isNew */
  function contentCard(content, isNew) {
    const card = document.createElement("div");
    const role = content.role || "?";
    card.className = "br-i-turncard " + role + (isNew ? " new" : "");
    const tag = document.createElement("div");
    tag.className = "role";
    tag.textContent = role + (isNew ? " · this turn" : "");
    card.append(tag);
    let imgs = null;
    for (const part of content.parts || []) {
      if (part.inlineData) {
        if (!imgs) {
          imgs = document.createElement("div");
          imgs.className = "imgs";
          card.append(imgs);
        }
        const img = document.createElement("img");
        img.src = `data:${part.inlineData.mimeType || "image/jpeg"};base64,${part.inlineData.data}`;
        imgs.append(img);
      } else if (part.functionCall) {
        const div = document.createElement("div");
        div.className = "fn";
        div.textContent = `functionCall ${part.functionCall.name}(${JSON.stringify(part.functionCall.args ?? {})})`;
        card.append(div);
      } else if (part.functionResponse) {
        const div = document.createElement("div");
        div.className = "fn";
        div.textContent =
          `functionResponse ${part.functionResponse.name} → ` +
          `${JSON.stringify(part.functionResponse.response?.outcome ?? part.functionResponse.response ?? "")}`;
        card.append(div);
      } else {
        const div = document.createElement("div");
        div.className = "txt";
        div.textContent = part.text ?? JSON.stringify(part);
        card.append(div);
      }
      if (part.thoughtSignature) {
        const sig = document.createElement("div");
        sig.className = "fn sig";
        sig.textContent = `[thoughtSignature · ${String(part.thoughtSignature).length} chars]`;
        card.append(sig);
      }
    }
    return card;
  }

  function renderInspector() {
    const rec = turns.get(inspecting);
    if (!rec) return;
    const { start, end, request: req } = rec;
    const n = (start.frames || []).length;
    // Count history images straight from the request body when we have it.
    const reqImgs = req
      ? (req.contents || []).reduce(
          (a, /** @type {any} */ c) => a + (c.parts || []).filter((/** @type {any} */ p) => p.inlineData).length,
          0,
        )
      : 0;
    const nHist = req ? reqImgs - n : (start.history_images ?? 0) * n;
    $(".br-i-title").textContent = `turn ${start.turn}`;
    $(".br-i-meta").textContent =
      `${n} new image${n === 1 ? "" : "s"}` +
      (nHist > 0 ? ` + ${nHist} in history` : "") +
      ` · history ${start.history} entries · ` +
      (end ? `${end.latency.toFixed(1)}s think` : "thinking…");

    const fr = $(".br-i-frames");
    fr.innerHTML = "";
    for (const f of start.frames || []) {
      const fig = document.createElement("figure");
      fig.innerHTML = `<img alt=""><figcaption></figcaption>`;
      /** @type {HTMLImageElement} */ (fig.querySelector("img")).src = "data:image/jpeg;base64," + f.jpeg;
      /** @type {HTMLElement} */ (fig.querySelector("figcaption")).textContent = f.label;
      fr.append(fig);
    }

    // The request itself: every history entry and the new message, verbatim.
    const conv = $(".br-i-conv");
    conv.innerHTML = "";
    $(".br-i-conv-h").hidden = /** @type {HTMLElement} */ (conv).hidden = !req;
    if (req) {
      const cfg = document.createElement("div");
      cfg.className = "br-i-cfg";
      cfg.textContent = `generationConfig ${JSON.stringify(req.generationConfig ?? {})}`;
      conv.append(cfg);
      const contents = req.contents || [];
      contents.forEach((/** @type {any} */ c, /** @type {number} */ i) =>
        conv.append(contentCard(c, i === contents.length - 1)),
      );
      const decl = document.createElement("details");
      decl.className = "br-i-json";
      decl.innerHTML = `<summary>tool declarations (json)</summary><pre></pre>`;
      /** @type {HTMLElement} */ (decl.querySelector("pre")).textContent = JSON.stringify(req.tools ?? [], null, 2);
      conv.append(decl);
    }

    $(".br-i-input").textContent = start.input || "—";

    const tools = $(".br-i-tools");
    tools.innerHTML = "";
    for (const t of start.tools || []) {
      const chip = document.createElement("span");
      chip.className = "br-call";
      chip.textContent = t;
      tools.append(chip);
    }

    const out = $(".br-i-out");
    out.innerHTML = "";
    if (!end) {
      out.innerHTML = `<div class="br-empty">still thinking…</div>`;
    } else {
      if (end.thoughts) out.append(iBlock("thoughts", end.thoughts, "thought"));
      if (end.speech) out.append(iBlock("speech", end.speech, "speech"));
      for (const c of end.calls || []) {
        const args = c.args && Object.keys(c.args).length ? " " + JSON.stringify(c.args) : "";
        out.append(iBlock(`call · ${c.name}${args}`, `→ ${c.outcome || ""}`, "call"));
      }
      if (!out.children.length) out.innerHTML = `<div class="br-empty">no output — observed only</div>`;
    }

    $(".br-i-sys pre").textContent =
      req?.systemInstruction?.parts?.[0]?.text ||
      start.system ||
      "(system prompt not reported by this robot's trace)";
  }

  $(".br-inspect-btn").addEventListener("click", () => openInspector(shownTurn));
  $(".br-i-close").addEventListener("click", closeInspector);
  $(".br-inspect-back").addEventListener("click", closeInspector);
  /** @param {KeyboardEvent} e */
  const onKey = (e) => {
    // isTypingContext: Esc in the docked composer (IME/autocomplete dismiss)
    // must not flip the stage under the user.
    if (e.key !== "Escape" || !visible || isTypingContext()) return;
    if (inspecting) closeInspector();
    else opts.onRequestClose?.();
  };
  window.addEventListener("keydown", onKey);

  /** @param {string} src @param {string} caption */
  function showFrame(src, caption) {
    /** @type {HTMLImageElement} */ ($(".br-frame")).src = src;
    $(".br-stage-idle").style.display = "none";
    $(".br-frame-cap").innerHTML = `<b>${caption}</b>`;
    $(".br-vision-src").textContent = S.haveTrace ? "turn-exact frames · /brain/trace" : "live · " + CAM_TOPIC;
  }

  function sweep() {
    const scan = $(".br-scan");
    scan.classList.remove("run");
    void scan.offsetWidth;
    scan.classList.add("run");
  }

  // Motion woke the brain: a soft glow + tag on the vision stage, nothing modal.
  let motionTimer = 0;
  function motionCue() {
    const stage = $(".br-stage");
    stage.classList.remove("motion");
    void stage.offsetWidth; // restart the animation if motion re-fires quickly
    stage.classList.add("motion");
    window.clearTimeout(motionTimer);
    motionTimer = window.setTimeout(() => stage.classList.remove("motion"), 1700);
  }

  let pingTimer = 0;
  /** @param {number} x1000 @param {number} y1000 */
  function showPing(x1000, y1000) {
    // Position against the contain-fitted image, not the (letterboxed) stage.
    const img = /** @type {HTMLImageElement} */ ($(".br-frame"));
    const stage = $(".br-stage");
    const iw = img.naturalWidth || 4, ih = img.naturalHeight || 3;
    const scale = Math.min(stage.clientWidth / iw, stage.clientHeight / ih);
    const w = iw * scale, h = ih * scale;
    const ping = $(".br-ping");
    ping.style.left = (stage.clientWidth - w) / 2 + (x1000 / 1000) * w + "px";
    ping.style.top = (stage.clientHeight - h) / 2 + (y1000 / 1000) * h + "px";
    ping.classList.add("on");
    window.clearTimeout(pingTimer);
    pingTimer = window.setTimeout(hidePing, 8000);
  }
  function hidePing() {
    $(".br-ping").classList.remove("on");
  }

  // ---- animation loop ------------------------------------------------------
  let raf = 0;
  function tickUI() {
    const t = performance.now();
    const timer = $(".br-timer");
    const ring = $(".br-ring-count");

    if (S.phase === "think") {
      S.cometAngle += 2.6;
      const rad = (S.cometAngle * Math.PI) / 180;
      const set = (/** @type {string} */ sel, /** @type {number} */ lag) => {
        const lr = ((S.cometAngle - lag) * Math.PI) / 180;
        const el = $(sel);
        el.setAttribute("cx", String(150 + RING_R * Math.cos(lag ? lr : rad)));
        el.setAttribute("cy", String(150 + RING_R * Math.sin(lag ? lr : rad)));
      };
      set(".br-comet", 0);
      set(".br-trail.t1", 9);
      set(".br-trail.t2", 17);
      timer.textContent = ((t - S.phaseAt) / 1000).toFixed(1);
      ring.style.strokeDashoffset = "0";
      ring.style.stroke = "transparent";
    } else if (S.phase === "wait" || S.phase === "error") {
      const left = Math.max(0, S.waitUntil - t);
      timer.textContent = (left / 1000).toFixed(1);
      ring.style.strokeDashoffset = String(RING_C * (1 - left / Math.max(S.waitTotal, 1)));
    } else if (S.phase === "look" || S.phase === "act") {
      timer.textContent = "·";
    } else {
      timer.textContent = "—";
    }

    const sub = $(".br-loop-sub");
    if (S.phase !== "error" && sub.classList.contains("err") && S.streak === 0) {
      sub.textContent = "";
      sub.classList.remove("err");
    }
    if (S.running) {
      sub.textContent = "▶ " + S.running;
      sub.classList.remove("err");
    } else if (S.phase !== "error" && sub.textContent.startsWith("▶")) sub.textContent = "";

    $(".br-turn").textContent = "TURN " + S.turn;
    if (S.uptimeAt) {
      const up = S.uptime + (t - S.uptimeAt) / 1000;
      $(".br-tile-up").textContent =
        up >= 3600
          ? (up / 3600).toFixed(1) + "h"
          : up >= 60
            ? Math.floor(up / 60) + "m" + String(Math.floor(up % 60)).padStart(2, "0")
            : Math.floor(up) + "s";
    }
    raf = requestAnimationFrame(tickUI);
  }
  raf = requestAnimationFrame(tickUI);

  // ---- data in -------------------------------------------------------------
  /** @param {any} msg */
  const asJson = (msg) => {
    try {
      return JSON.parse(msg.data);
    } catch {
      return null;
    }
  };
  /** @param {(d: any) => void} fn */
  const jsonHandler = (fn) => (/** @type {any} */ msg) => {
    const d = asJson(msg);
    if (d) fn(d);
  };

  const handlers = { onTrace, onSkill, onAgentStatus, onBrainStatus, setPose, setSpeaking };

  /** @type {(() => void)[]} */
  let unsubs = [];
  /** @type {(() => void) | null} */
  let camUnsub = null;
  /** @type {(() => void) | null} */
  let demoStop = null;
  let destroyed = false;
  const demoMode = new URLSearchParams(location.search).has("demo");

  // The live-camera fallback pulls base64 JPEGs over rosbridge, on a page that
  // already streams WebRTC video — so it runs only while the monitor is on
  // screen (see setVisible), unlike the cheap trace/status feeds.
  const subscribeCam = () =>
    ros.subscribe(
      CAM_TOPIC,
      (msg) => {
        if (!S.haveTrace) showFrame("data:image/jpeg;base64," + msg.data, "live camera");
      },
      500,
      "sensor_msgs/msg/CompressedImage",
    );

  if (demoMode) {
    // No robot in the picture: the socket's connect card would just sit over
    // the synthetic show. Restored while the monitor is hidden (setVisible).
    document.querySelector(".connect-layer")?.classList.add("br-hidden");
    void import("./demo.js").then((m) => {
      if (destroyed) return; // the page unmounted before the chunk loaded
      demoStop = m.startDemo(handlers);
    });
  } else {
    unsubs = [
      ros.subscribe("/brain/trace", jsonHandler(onTrace), 0, "std_msgs/msg/String"),
      ros.subscribe("/brain/agent_status", jsonHandler(onAgentStatus), 0, "std_msgs/msg/String"),
      ros.subscribe("/brain/websocket_status", jsonHandler(onBrainStatus), 0, "std_msgs/msg/String"),
      ros.subscribe("/brain/skill_status_update", jsonHandler(onSkill), 0, "std_msgs/msg/String"),
      ros.subscribe("/tts/is_playing", (msg) => setSpeaking(msg.data === "true"), 0, "std_msgs/msg/String"),
      ros.subscribe(
        "/odom",
        (msg) => {
          const p = msg.pose.pose.position, q = msg.pose.pose.orientation;
          setPose(p.x, p.y, Math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z)));
        },
        200,
        "nav_msgs/msg/Odometry",
      ),
    ];
    camUnsub = subscribeCam();
  }

  /** @param {boolean} v */
  function setVisible(v) {
    if (v === visible) return;
    visible = v;
    if (demoMode) document.querySelector(".connect-layer")?.classList.toggle("br-hidden", v);
    if (!v) {
      cancelAnimationFrame(raf);
      camUnsub?.();
      camUnsub = null;
      return;
    }
    raf = requestAnimationFrame(tickUI);
    if (!demoMode) camUnsub = subscribeCam();
  }

  return {
    setVisible,
    destroy() {
      destroyed = true;
      cancelAnimationFrame(raf);
      window.clearTimeout(pingTimer);
      window.clearTimeout(motionTimer);
      for (const id of uiTimers) window.clearTimeout(id);
      window.removeEventListener("keydown", onKey);
      unsubs.forEach((u) => u());
      camUnsub?.();
      demoStop?.();
      document.querySelector(".connect-layer")?.classList.remove("br-hidden");
      root.innerHTML = "";
    },
  };
}

function template() {
  return `
  <div class="br-head">
    <h1 class="page-title">Brain</h1>
    <span class="br-esc-hint"><kbd>esc</kbd> closes</span>
    <div class="br-chips">
      <div class="br-chip br-chip-active" title="/brain/agent_status"><span class="led"></span><span>brain <b>—</b></span></div>
      <div class="br-chip br-chip-directive" title="current directive — /brain/agent_status"><span>directive</span><b>—</b></div>
      <div class="br-chip br-chip-backend" title="inference backend — /brain/trace, falling back to /brain/websocket_status"><span>backend</span><b>—</b></div>
      <div class="br-chip br-chip-model" title="model in use — /brain/trace"><span>model</span><b>—</b></div>
      <div class="br-chip br-chip-voice" title="pulses while the robot speaks — /tts/is_playing"><span class="led"></span><span>voice</span></div>
    </div>
  </div>
  <div class="br-grid">
    <section class="br-panel br-panel-loop">
      <h2>The agent loop <span class="sub br-tools" title="Tools the model may call this turn"></span></h2>
      <svg class="br-loop-svg" viewBox="0 0 300 300">
        <circle class="br-ring-base" cx="150" cy="150" r="${RING_R}"/>
        <circle class="br-ring-count" cx="150" cy="150" r="${RING_R}"/>
        <circle class="br-trail t2" r="2.5"/>
        <circle class="br-trail t1" r="3.5"/>
        <circle class="br-comet" r="5"/>
        <g class="br-node look"><title>Capture the camera frames sent to the model</title><circle r="27"/><text dy="4">LOOK</text></g>
        <g class="br-node think"><title>Waiting on model inference</title><circle r="27"/><text dy="4">THINK</text></g>
        <g class="br-node act"><title>Executing the tool calls the model returned</title><circle r="27"/><text dy="4">ACT</text></g>
        <g class="br-center">
          <text class="br-phase" x="150" y="130">OFFLINE</text>
          <text class="br-timer" x="150" y="168"><title>seconds spent thinking, or the countdown to the next look</title>—</text>
          <text class="br-turn"  x="150" y="192">TURN 0</text>
        </g>
      </svg>
      <div class="br-loop-sub"></div>
      <div class="br-streak" title="Consecutive inference failures"></div>
    </section>

    <section class="br-panel br-panel-vision">
      <h2>Robot vision <span class="sub br-vision-src">waiting for frames</span></h2>
      <div class="br-stage">
        <img class="br-frame" alt="">
        <div class="br-stage-idle">NO SIGNAL</div>
        <div class="br-scan"></div>
        <div class="br-motion-tag" title="Motion woke the brain early — see the event queue">◉ motion</div>
        <div class="br-ping" title="Where the model asked the robot to drive (go_to_point)"><span class="ringA"></span><span class="ringB"></span><span class="cross"></span></div>
      </div>
      <div class="br-film"></div>
      <div class="br-vision-cap">
        <span class="br-frame-cap"></span>
        <button class="br-inspect-btn" hidden title="Everything the model received and returned this turn">inspect turn</button>
        <span class="br-pose-strip" title="robot pose — /odom">
          <svg class="br-heading" viewBox="0 0 16 16"><polygon class="br-needle" points="8,1.5 11,12 8,9.5 5,12"/></svg>
          <span class="br-pose-txt">pose —</span>
        </span>
      </div>
    </section>

    <section class="br-panel br-panel-actions">
      <h2>Turns &amp; actions <span class="sub">tool calls and skill runs</span></h2>
      <div class="br-feed br-actions"></div>
    </section>

    <section class="br-panel br-panel-queue">
      <h2>Event queue <span class="sub">what the next look will carry</span></h2>
      <div class="br-feed br-queue"><div class="br-empty">quiet — heartbeat looks only</div></div>
    </section>

    <section class="br-panel br-panel-vitals">
      <h2>Vitals <span class="sub">think latency · s</span></h2>
      <div class="br-tiles">
        <div class="br-tile" title="Loop turns since the brain started"><div class="v br-tile-turns">0</div><div class="l">turns</div></div>
        <div class="br-tile" title="Latest model inference latency"><div class="v br-tile-last">—</div><div class="l">last think</div></div>
        <div class="br-tile" title="Average model inference latency this session"><div class="v br-tile-avg">—</div><div class="l">avg think</div></div>
        <div class="br-tile" title="How long the brain loop has been running"><div class="v br-tile-up">—</div><div class="l">uptime</div></div>
      </div>
      <div class="br-spark-wrap">
        <svg class="br-spark" viewBox="0 0 400 88" preserveAspectRatio="none"></svg>
        <div class="br-spark-tip"></div>
      </div>
      <div class="br-gauge">
        <div class="bar"><div class="fill br-hist-fill"></div></div>
        <div class="lab" title="Entries in the rolling conversation history the model sees"><span>conversation history</span><span class="br-hist-txt">0 / ${HIST_MAX}</span></div>
      </div>
    </section>
  </div>
  <div class="br-inspect" hidden>
    <div class="br-inspect-back"></div>
    <div class="br-inspect-card">
      <div class="br-i-head">
        <b class="br-i-title">turn</b><span class="br-i-meta"></span>
        <button class="br-i-close" aria-label="Close" title="Close">✕</button>
      </div>
      <div class="br-i-body">
        <h3>new images this turn</h3>
        <div class="br-i-frames"></div>
        <h3>input text</h3>
        <pre class="br-i-input"></pre>
        <h3>tools armed</h3>
        <div class="br-i-tools"></div>
        <h3 class="br-i-conv-h" hidden>full request — everything sent to gemini</h3>
        <div class="br-i-conv" hidden></div>
        <h3>model output</h3>
        <div class="br-i-out"></div>
        <details class="br-i-sys"><summary>system instruction</summary><pre></pre></details>
      </div>
    </div>
  </div>`;
}
