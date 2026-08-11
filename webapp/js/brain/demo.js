// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Scripted synthetic brain for the Brain page (?demo) — drives the page's
// handlers through an idle→command→navigate→error→recover story so the UI can
// be exercised with no robot. Loaded lazily; never imported on the real path.

const HIST_MAX = 60;

// Abbreviated stand-in for the real system instruction (brain/prompt.py).
const DEMO_SYSTEM = `You are the brain of an Innate home robot: a small wheeled base with a camera, \
a robotic arm, and a speaker. You run on the robot itself.

Each update you receive contains the latest camera frame, the robot's state, and any new events \
(user speech, skill results, sensor input). You act by calling tools — the robot's skills. \
Anything you write as plain text is spoken aloud through the robot's speaker.

Your directive:
explore`;

/**
 * @param {{ onTrace: (d: any) => void, onSkill: (d: any) => void,
 *   onAgentStatus: (d: any) => void, onBrainStatus: (d: any) => void,
 *   setPose: (x: number, y: number, yaw: number) => void, setSpeaking: (on: boolean) => void }} h
 * @returns {() => void} stop
 */
export function startDemo(h) {
  let on = true;
  /** @type {number[]} */
  const timers = [];
  /** @param {number} ms */
  const sleep = (ms) => new Promise((r) => timers.push(window.setTimeout(r, ms)));

  h.onAgentStatus({ brain_active: true, current_directive: "explore", active_skills: [], tts_available: true });
  h.onBrainStatus({ backend: "innate-proxy" });

  /** @type {{turn: number, history: number, queued: {kind: string, text: string}[], running: string | null,
   *   streak: number, inFlight: boolean, nextIn: number, uptime: number}} */
  const D = { turn: 0, history: 6, queued: [], running: null, streak: 0, inFlight: false, nextIn: 3, uptime: 47 };
  const snap = () =>
    h.onTrace({
      ev: "snapshot", active: true, backend: "innate-proxy", model: "gemini-3.6-flash",
      turn: D.turn, in_flight: D.inFlight, queued: D.queued, next_in: D.nextIn,
      streak: D.streak, running: D.running, history: D.history, uptime: D.uptime++,
    });
  timers.push(window.setInterval(() => on && snap(), 1000));

  let px = 0;
  timers.push(
    window.setInterval(() => {
      if (on) h.setPose(1.1 + 0.28 * Math.sin(px / 9), 0.4 + 0.2 * Math.cos(px / 13), 0.6 * Math.sin(px / 17));
      px++;
    }, 500),
  );

  /** The input text a real robot would trace: status line, queued events, image note. */
  const inputText = () =>
    [
      `[t+${D.uptime}s] pose: x=1.10m y=0.40m heading=12°${D.running ? ` | running skill: ${D.running}` : ""}`,
      ...D.queued.map((q) => `- ${q.text}`),
      "(second image is the arm wrist camera)",
    ].join("\n");

  /** @param {boolean} withSock */
  const frames = (withSock) => [
    { label: "head camera", jpeg: frame(withSock) },
    { label: "wrist camera", jpeg: wristFrame() },
  ];

  /** Rolling request history, pruned like the real session: images survive
   * only in the newest 3 user turns.
   * @type {any[]} */
  const contents = [];
  /** @param {any} userContent @param {any} modelContent */
  const commit = (userContent, modelContent) => {
    contents.push(userContent, modelContent);
    const imgTurns = contents.filter((c) => c.role === "user" && c.parts.some((/** @type {any} */ p) => p.inlineData));
    for (const c of imgTurns.slice(0, -3))
      c.parts = c.parts.map((/** @type {any} */ p) => (p.inlineData ? { text: "[older camera frame removed]" } : p));
    while (contents.length > 12) contents.shift();
  };

  const TOOLS = ["navigate_to_position", "wave", "pick_up_sock", "go_to_point", "wait"];

  /** @param {{withSock: boolean, think: number, thoughts: string | null, speech?: string | null, calls?: any[]}} opts */
  const turn = async ({ withSock, think, thoughts, speech = null, calls }) => {
    D.turn++;
    D.inFlight = true;
    const fr = frames(withSock);
    const input = inputText();
    h.onTrace({
      ev: "turn_start", turn: D.turn, input, images: 2,
      tools: TOOLS,
      history: D.history, history_images: Math.min(D.turn - 1, 3),
      system: DEMO_SYSTEM, frames: fr,
    });
    const userContent = {
      role: "user",
      parts: [{ text: input }, ...fr.map((f) => ({ inlineData: { mimeType: "image/jpeg", data: f.jpeg } }))],
    };
    h.onTrace({
      ev: "turn_request", turn: D.turn,
      body: {
        systemInstruction: { parts: [{ text: DEMO_SYSTEM }] },
        contents: [...contents.map((c) => ({ ...c })), userContent],
        generationConfig: { thinkingConfig: { includeThoughts: true, thinkingLevel: "minimal" } },
        tools: [{ functionDeclarations: TOOLS.map((name) => ({ name, description: `demo declaration for ${name}` })) }],
      },
    });
    D.queued = [];
    await sleep(think);
    if (!on) return;
    D.inFlight = false;
    D.history = Math.min(D.history + 3, HIST_MAX);
    const theCalls = calls || [{ name: "wait", args: {}, outcome: "ok" }];
    commit(userContent, {
      role: "model",
      parts: [
        ...(speech ? [{ text: speech }] : []),
        ...theCalls.map((c) => ({ functionCall: { name: c.name, args: c.args } })),
      ],
    });
    h.onTrace({
      ev: "turn_end", turn: D.turn, latency: think / 1000, thoughts, speech,
      calls: theCalls, history: D.history, next_in: D.nextIn,
    });
    if (speech) {
      h.setSpeaking(true);
      timers.push(window.setTimeout(() => h.setSpeaking(false), 2200));
    }
  };

  void (async () => {
    await sleep(600);
    while (on) {
      await turn({ withSock: false, think: 1700, thoughts: "The room is unchanged — couch, shelf, rug. Nothing needs my attention." });
      await sleep(3200); if (!on) return;
      await turn({ withSock: false, think: 1400, thoughts: "Still quiet. I'll keep watching." });
      await sleep(2100); if (!on) return;

      // Someone walks into view: the motion gate wakes the brain early.
      const moved = "Motion detected in the camera view — something or someone is moving nearby.";
      h.onTrace({ ev: "event", kind: "motion", text: moved });
      D.queued = [{ kind: "motion", text: moved }];
      await sleep(1100); if (!on) return;
      await turn({ withSock: false, think: 1500, thoughts: "Something just moved at the edge of my view — the user is coming closer. I'll stay ready." });
      await sleep(1400); if (!on) return;

      const ask = "Bring me the sock by the couch";
      h.onTrace({ ev: "event", kind: "user", text: `The user says: "${ask}"` });
      D.queued = [{ kind: "user", text: `The user says: "${ask}"` }];
      await sleep(1400); if (!on) return;

      await turn({
        withSock: true, think: 2900,
        thoughts: "The user wants the sock. I can see a white sock on the floor to the right of the rug, roughly 1.6 m ahead. I'll point at its base and drive there before attempting the pick.",
        speech: "On it — I can see the sock near the rug.",
        calls: [{ name: "go_to_point", args: { x: 620, y: 700 }, outcome: "driving to the floor point 1.6m away (stopping ~0.35m short) — you will get an event when it finishes" }],
      });
      D.running = "navigate_to_position";
      D.nextIn = 5;
      h.onSkill({ skill_name: "navigate_to_position", primitive_id: "demo-nav-" + D.turn, status: "running" });
      await sleep(2600); if (!on) return;

      await turn({ withSock: true, think: 1600, thoughts: "Navigation is progressing; the sock is getting closer to the bottom of the frame. No correction needed." });
      await sleep(2400); if (!on) return;
      h.onSkill({ skill_name: "navigate_to_position", primitive_id: "demo-nav-" + D.turn, status: "completed" });
      h.onTrace({ ev: "event", kind: "info", text: "Skill navigate_to_position completed" });
      D.running = null;
      D.nextIn = 3;
      await sleep(1200); if (!on) return;

      await turn({
        withSock: true, think: 2200,
        thoughts: "I've arrived at the standoff point. The sock is right in front of me — task done from the navigation side.",
        speech: "Here's the sock — right in front of me now.",
      });
      await sleep(2800); if (!on) return;

      // A transient inference failure, then recovery.
      D.turn++;
      D.inFlight = true;
      h.onTrace({ ev: "turn_start", turn: D.turn, input: inputText(), images: 2, tools: ["wait"], history: D.history, system: DEMO_SYSTEM, frames: frames(true) });
      await sleep(900); if (!on) return;
      D.inFlight = false;
      D.streak = 1;
      h.onTrace({ ev: "turn_error", turn: D.turn, error: "gemini via proxy: HTTP 503: overloaded", streak: 1, backoff: 5 });
      await sleep(5100); if (!on) return;
      D.streak = 0;
      await turn({ withSock: true, think: 1500, thoughts: "Back online. Scene unchanged." });
      await sleep(3000);
    }
  })();

  return () => {
    on = false;
    timers.forEach((t) => {
      window.clearTimeout(t);
      window.clearInterval(t);
    });
  };
}

/**
 * A synthetic living-room frame (base64 JPEG body) — wall, floor, couch,
 * shelf, rug, and optionally the sock the story is about.
 * @param {boolean} withSock
 */
function frame(withSock) {
  const c = Object.assign(document.createElement("canvas"), { width: 640, height: 480 });
  const g = /** @type {CanvasRenderingContext2D} */ (c.getContext("2d"));
  const wall = g.createLinearGradient(0, 0, 0, 210);
  wall.addColorStop(0, "#31343c");
  wall.addColorStop(1, "#3c404a");
  g.fillStyle = wall;
  g.fillRect(0, 0, 640, 210);
  const floor = g.createLinearGradient(0, 210, 0, 480);
  floor.addColorStop(0, "#5a4a3c");
  floor.addColorStop(1, "#79634e");
  g.fillStyle = floor;
  g.fillRect(0, 210, 640, 270);
  g.strokeStyle = "rgba(0,0,0,0.18)";
  g.lineWidth = 2;
  for (let i = 0; i < 7; i++) {
    const y = 230 + i * 38;
    g.beginPath();
    g.moveTo(0, y);
    g.lineTo(640, y);
    g.stroke();
  }
  g.fillStyle = "#46586b";
  g.fillRect(40, 120, 190, 130); // couch
  g.fillStyle = "#54687e";
  g.fillRect(40, 100, 190, 34);
  g.fillStyle = "#2c2f36";
  g.fillRect(470, 60, 120, 165); // shelf
  g.fillStyle = "#c9a86a";
  g.fillRect(480, 92, 100, 9);
  g.fillRect(480, 142, 100, 9);
  g.fillStyle = "#7d3b3b";
  g.beginPath();
  g.ellipse(390, 330, 130, 42, 0, 0, 7);
  g.fill(); // rug
  if (withSock) {
    g.fillStyle = "#e8e6df";
    g.beginPath();
    g.ellipse(396, 336, 22, 9, -0.5, 0, 7);
    g.fill();
    g.beginPath();
    g.ellipse(408, 326, 9, 12, 0.4, 0, 7);
    g.fill();
  }
  g.fillStyle = "rgba(255,255,255,0.05)";
  g.fillRect(0, 0, 640, 480);
  return c.toDataURL("image/jpeg", 0.8).split(",")[1];
}

/** The arm wrist camera's view (base64 JPEG body) — floor close-up between
 * the two gripper fingers. */
function wristFrame() {
  const c = Object.assign(document.createElement("canvas"), { width: 640, height: 480 });
  const g = /** @type {CanvasRenderingContext2D} */ (c.getContext("2d"));
  const floor = g.createLinearGradient(0, 0, 0, 480);
  floor.addColorStop(0, "#6b573f");
  floor.addColorStop(1, "#86705a");
  g.fillStyle = floor;
  g.fillRect(0, 0, 640, 480);
  g.strokeStyle = "rgba(0,0,0,0.22)";
  g.lineWidth = 3;
  for (let i = 0; i < 5; i++) {
    const y = 60 + i * 95;
    g.beginPath();
    g.moveTo(0, y);
    g.lineTo(640, y);
    g.stroke();
  }
  g.fillStyle = "#23252a"; // gripper fingers, bottom corners
  g.beginPath();
  g.moveTo(0, 480); g.lineTo(150, 480); g.lineTo(85, 330); g.lineTo(0, 360);
  g.fill();
  g.beginPath();
  g.moveTo(640, 480); g.lineTo(490, 480); g.lineTo(555, 330); g.lineTo(640, 360);
  g.fill();
  g.fillStyle = "rgba(255,255,255,0.06)";
  g.fillRect(0, 0, 640, 480);
  return c.toDataURL("image/jpeg", 0.8).split(",")[1];
}
