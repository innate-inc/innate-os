// @ts-check
// Skill rendering helpers for the agent chat stream — naming, argument
// formatting, and the small DOM builders for a skill card's detail sections.
//
// Pure and DOM-local: nothing here reads panel state, so the chat stream can
// call these freely while owning all the mutable transcript state itself.

/** @param {string} name */
export function skillDisplayName(name) {
  return name.replace(/_/g, " ");
}

/** @param {string} name */
export function skillNameKey(name) {
  return skillDisplayName(name).toLowerCase();
}

/** @param {Element[]} wraps */
export function skillGroupStatus(wraps) {
  const order = ["running", "failed", "interrupted", "completed"];
  const present = new Set();
  for (const wrap of wraps) {
    const status = order.find((cls) => wrap.classList.contains(cls));
    if (status) present.add(status);
  }
  return order.find((cls) => present.has(cls)) || "completed";
}

/** @param {string} label */
export function modeButton(label) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "agent-stream-mode-btn";
  btn.textContent = label;
  return btn;
}

/** @param {{wrap: HTMLElement, head: HTMLButtonElement}} run @param {boolean} open */
export function setSkillRunOpen(run, open) {
  setSkillElementOpen(run.wrap, run.head, open);
}

/** @param {HTMLElement} wrap @param {HTMLButtonElement} head @param {boolean} open */
export function setSkillElementOpen(wrap, head, open) {
  wrap.classList.toggle("open", open);
  head.setAttribute("aria-expanded", String(open));
  head.title = open ? "Hide skill details" : "Show skill details";
}

/**
 * @param {string} name
 * @param {any} args
 * @returns {{ summary: string, rows: Array<{label: string, value: string}> }}
 */
export function formatSkillArgs(name, args) {
  if (!args || typeof args !== "object" || Array.isArray(args)) return { summary: "", rows: [] };
  const entries = Object.entries(args)
    .filter(([, v]) => v !== null && v !== undefined && v !== "")
    .sort(([a], [b]) => skillArgOrder(a) - skillArgOrder(b));
  if (!entries.length) return { summary: "", rows: [] };
  const rows = entries.map(([key, value]) => ({
    label: skillArgLabel(key),
    value: skillArgValue(name, key, value),
  }));
  return { summary: skillArgSummary(name, args, rows), rows };
}

/** @param {HTMLElement} container @param {Array<{label: string, value: string}>} rows */
export function renderSkillParameters(container, rows) {
  const label = document.createElement("p");
  label.className = "chat-skill-detail-label";
  label.textContent = "Parameters";
  const list = document.createElement("dl");
  for (const row of rows) {
    const term = document.createElement("dt");
    term.textContent = row.label;
    const value = document.createElement("dd");
    value.textContent = row.value;
    list.append(term, value);
  }
  container.replaceChildren(label, list);
}

/** @param {HTMLElement} container @param {string} reason */
export function renderSkillFailure(container, reason) {
  const label = document.createElement("p");
  label.className = "chat-skill-detail-label";
  label.textContent = "Failure";
  const message = document.createElement("p");
  message.className = "chat-skill-failure-message";
  message.textContent = reason;
  container.replaceChildren(label, message);
}

/** @param {string} status */
export function skillStatusLabel(status) {
  return {
    running: "Running",
    completed: "Completed",
    failed: "Failed",
    interrupted: "Interrupted",
  }[status] || "Running";
}

/** @param {string} key */
function skillArgOrder(key) {
  const order = [
    "x",
    "y",
    "z",
    "theta_degrees",
    "angle_degrees",
    "local_frame",
    "speed",
    "distance",
  ];
  const index = order.indexOf(key);
  return index === -1 ? order.length : index;
}

/** @param {string} key */
function skillArgLabel(key) {
  const labels = {
    theta_degrees: "Heading",
    angle_degrees: "Angle",
    local_frame: "Frame",
    num_loops: "Loops",
    points_per_loop: "Points per loop",
    duration_per_point: "Point duration",
    is_capture: "Capture",
    keep_gripper: "Keep gripper",
  };
  if (key in labels) return labels[/** @type {keyof typeof labels} */ (key)];
  if (/^[xyz]$/.test(key)) return key.toUpperCase();
  const text = key.replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** @param {string} name @param {string} key @param {any} value */
function skillArgValue(name, key, value) {
  if (key === "local_frame") return value ? "Local" : "Map";
  if (key === "is_capture" || key === "keep_gripper") return value ? "Yes" : "No";
  if (typeof value === "object") return roundNums(JSON.stringify(value));
  if (typeof value !== "number") return String(value).replace(/_/g, " ");
  const number = roundNums(String(value));
  if (key === "theta_degrees" || key === "angle_degrees") return `${number}°`;
  if (/^(x|y|z|center_x|center_y|center_z|radius|distance)$/.test(key)) return `${number} m`;
  if (key.includes("duration")) return `${number} s`;
  if (key === "speed" && name.replace(/_/g, " ").includes("turn")) return `${number} rad/s`;
  if (key === "speed" && name.replace(/_/g, " ").includes("move")) return `${number} m/s`;
  return number;
}

/** @param {string} name @param {Record<string, any>} args @param {Array<{label: string, value: string}>} rows */
function skillArgSummary(name, args, rows) {
  const skill = name.replace(/_/g, " ").toLowerCase();
  if (skill.includes("pick") && typeof args.prompt === "string") return args.prompt;
  if (skill.includes("navigate to position")) {
    return `x ${roundNums(String(args.x))} m · y ${roundNums(String(args.y))} m · ${roundNums(String(args.theta_degrees ?? 0))}° · ${args.local_frame ? "Local" : "Map"}`;
  }
  if (skill.includes("move straight")) {
    const distance = Number(args.distance);
    const speed = args.speed == null ? "" : ` · ${roundNums(String(args.speed))} m/s`;
    return `${roundNums(String(Math.abs(distance)))} m ${distance < 0 ? "backward" : "forward"}${speed}`;
  }
  if (skill.includes("turn in place")) {
    const angle = Number(args.angle_degrees);
    const speed = args.speed == null ? "" : ` · ${roundNums(String(args.speed))} rad/s`;
    return `${roundNums(String(Math.abs(angle)))}° ${angle < 0 ? "right" : "left"}${speed}`;
  }
  if (skill.includes("head emotion")) {
    return `${String(args.emotion).replace(/_/g, " ")}${Number(args.repeat) > 1 ? ` ×${args.repeat}` : ""}`;
  }
  if (skill.includes("search memory")) return `“${String(args.query)}”`;
  if (skill.includes("arm move to xyz")) {
    return `x ${roundNums(String(args.x))} m · y ${roundNums(String(args.y))} m · z ${roundNums(String(args.z))} m`;
  }
  return rows
    .slice(0, 3)
    .map(({ label, value }) => `${label} ${value}`)
    .join(" · ");
}

/**
 * Shorten long decimals in a display string to 2 places. Cosmetic only.
 * @param {string} text
 */
export function roundNums(text) {
  return text.replace(/-?\d+\.\d+(?:[eE][-+]?\d+)?/g, (n) => {
    const v = Number(n);
    return Number.isFinite(v) ? String(Math.round(v * 100) / 100) : n;
  });
}
