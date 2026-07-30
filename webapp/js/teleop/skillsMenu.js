// @ts-check
// Skills menu — a compact, collapsible skill launcher anchored to the bottom bar
// next to the TTS input. Replaces the old right-dock Skills panel: it shows the
// live roster from /brain/available_skills as a dropdown of rows. A skill with
// no parameters runs on a single click; a parameterized skill expands inline to
// a small form with a Run button. Execution goes through the /execute_skill
// action (cancelable, with streamed feedback), same as the old panel.

import {
  AVAILABLE_SKILLS_TOPIC,
  CANCEL_SKILL_SERVICE,
  EXECUTE_SKILL_ACTION,
  EXECUTE_SKILL_ACTION_TYPE,
  PINNED_SKILLS,
  SKILL_STATUS_UPDATE_TOPIC,
} from "../constants.js";

/**
 * @param {HTMLElement} parent The bottom-bar overlay (shared with the TTS bar).
 * @param {import("../rosClient.js").RosClient} rosClient
 * @returns {{ destroy: () => void }}
 */
export function createSkillsMenu(parent, rosClient) {
  const menu = document.createElement("div");
  menu.className = "skills-menu";

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "skills-menu-btn";
  const btnDot = document.createElement("span");
  btnDot.className = "skills-menu-dot";
  const btnLabel = document.createElement("span");
  btnLabel.className = "skills-menu-label";
  btnLabel.textContent = "Skills";
  // Currently-running skill (from /brain/skill_status_update), or "None active".
  const btnActive = document.createElement("span");
  btnActive.className = "skills-menu-active";
  btnActive.textContent = "None active";
  const btnChevron = document.createElement("span");
  btnChevron.className = "skills-menu-chevron mono";
  btnChevron.textContent = "▾";
  btn.append(btnDot, btnLabel, btnActive, btnChevron);

  const pop = document.createElement("div");
  pop.className = "skills-pop";
  pop.appendChild(buildTypeLegend());
  const scrollEl = document.createElement("div");
  scrollEl.className = "skills-pop-scroll";
  const listEl = document.createElement("div");
  listEl.className = "skills-pop-list";
  scrollEl.appendChild(listEl);
  pop.appendChild(scrollEl);

  menu.append(pop, btn);
  parent.appendChild(menu);

  // ---- state --------------------------------------------------------------

  let open = false;
  /** @type {any[]} */
  let skills = [];
  let signature = "";
  /** @type {string | null} */
  let expandedId = null;
  /** Per-skill, per-param string values, kept across re-renders. @type {Map<string, Record<string, string>>} */
  const inputValues = new Map();
  /** Last/in-flight run. `done` marks the terminal state. @type {{ skillId: string, cancel: () => void, text: string, error: boolean, canceling: boolean, done: boolean } | null} */
  let run = null;
  /** Skill the robot reports running via /brain/skill_status_update (covers
   *  agent-driven runs too, not just ones launched from this menu). */
  let topicActiveName = "";
  /** Stop requested (via /brain/cancel_skill) for a run started elsewhere;
   *  cleared when the status topic reports the run over. */
  let externCanceling = false;

  // ---- skill input schema (mirrors the sim console) -----------------------

  /** @param {any} skill @returns {Record<string, any>} */
  function getSkillInputs(skill) {
    if (skill?.inputs && typeof skill.inputs === "object" && !Array.isArray(skill.inputs)) {
      return skill.inputs;
    }
    if (typeof skill?.inputs_json === "string" && skill.inputs_json) {
      try {
        const parsed = JSON.parse(skill.inputs_json);
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed;
      } catch {
        return {};
      }
    }
    return {};
  }

  /** @param {any} schema */
  function schemaType(schema) {
    const t = typeof schema === "string" ? schema : schema?.type;
    return String(t ?? "any").toLowerCase();
  }
  /** @param {any} schema */
  function isRequired(schema) {
    return typeof schema === "object" && schema?.required === true;
  }
  /** @param {any} schema @returns {any[]} */
  function enumValues(schema) {
    return typeof schema === "object" && Array.isArray(schema?.enum) ? schema.enum : [];
  }
  /** @param {string} t */
  const isNumeric = (t) => ["int", "integer", "number", "float", "double"].includes(t);
  /** @param {string} t */
  const isInt = (t) => t === "int" || t === "integer";
  /** @param {string} t */
  const isBool = (t) => t === "bool" || t === "boolean";
  /** @param {string} t */
  const isJson = (t) => ["json", "object", "dict"].includes(t);

  /** @param {any} skill */
  const hasParams = (skill) => Object.keys(getSkillInputs(skill)).length > 0;

  /**
   * Current string value for a param, falling back to the schema default.
   * @param {string} skillId @param {string} paramName @param {any} schema
   */
  function valueFor(skillId, paramName, schema) {
    const stored = inputValues.get(skillId)?.[paramName];
    if (stored !== undefined) return stored;
    if (typeof schema === "object" && schema?.default !== undefined) {
      return isJson(schemaType(schema)) ? JSON.stringify(schema.default) : String(schema.default);
    }
    return "";
  }
  /** @param {string} skillId @param {string} paramName @param {string} value */
  function setValue(skillId, paramName, value) {
    const map = inputValues.get(skillId) ?? {};
    map[paramName] = value;
    inputValues.set(skillId, map);
  }

  /**
   * Validate + coerce a skill's params into the inputs object the action wants.
   * @param {any} skill
   * @returns {{ inputs: Record<string, any> } | { error: string, param: string }}
   */
  function buildInputs(skill) {
    const schema = getSkillInputs(skill);
    /** @type {Record<string, any>} */
    const out = {};
    for (const [name, spec] of Object.entries(schema)) {
      const t = schemaType(spec);
      const raw = valueFor(skill.id, name, spec).trim();
      if (raw === "") {
        if (isRequired(spec)) return { error: "Required", param: name };
        continue; // optional + empty → let the skill default it
      }
      const options = enumValues(spec);
      if (options.length > 0) {
        const match = options.find((o) => String(o) === raw);
        if (match === undefined) return { error: "Not an allowed value", param: name };
        out[name] = match;
      } else if (isBool(t)) {
        out[name] = raw === "true";
      } else if (isInt(t)) {
        if (!/^-?\d+$/.test(raw)) return { error: "Whole number expected", param: name };
        out[name] = parseInt(raw, 10);
      } else if (isNumeric(t)) {
        const n = Number(raw.replace(",", "."));
        if (!Number.isFinite(n)) return { error: "Number expected", param: name };
        out[name] = n;
      } else if (isJson(t)) {
        try {
          out[name] = JSON.parse(raw);
        } catch {
          return { error: "Invalid JSON", param: name };
        }
      } else {
        out[name] = raw;
      }
    }
    return { inputs: out };
  }

  // ---- run lifecycle ------------------------------------------------------

  /** @param {any} skill */
  function startRun(skill) {
    const built = buildInputs(skill);
    if ("error" in built) {
      run = { skillId: skill.id, cancel: () => {}, text: `${built.param}: ${built.error}`, error: true, canceling: false, done: true };
      // A param error means the skill needs the form open to fix it.
      expandedId = skill.id;
      render();
      return;
    }
    const { promise, cancel } = rosClient.sendActionGoal(
      EXECUTE_SKILL_ACTION,
      EXECUTE_SKILL_ACTION_TYPE,
      { skill_type: skill.id, inputs: JSON.stringify(built.inputs) },
      {
        onFeedback: (values) => {
          if (!run || run.skillId !== skill.id || run.done) return;
          const fb = values?.feedback;
          if (typeof fb === "string" && fb) {
            run.text = fb;
            render();
          }
        },
      },
    );
    run = { skillId: skill.id, cancel, text: "Running…", error: false, canceling: false, done: false };
    render();
    // Collapse the launcher once a run is underway — it otherwise covers the
    // feed. The button's green dot + skill name keep showing what's active;
    // reopen to Stop it.
    setOpen(false);

    promise.then(
      (values) => {
        if (run?.skillId !== skill.id) return;
        const ok = values?.success !== false && values?.success_type !== "failure";
        run = {
          skillId: skill.id,
          cancel: () => {},
          text: values?.message || (values?.success_type === "cancelled" ? "Cancelled" : ok ? "Done" : "Failed"),
          error: !ok,
          canceling: false,
          done: true,
        };
        render();
      },
      (err) => {
        if (run?.skillId !== skill.id) return;
        run = { skillId: skill.id, cancel: () => {}, text: err?.message || "Run failed", error: true, canceling: false, done: true };
        render();
      },
    );
  }

  function stopRun() {
    if (!run || run.canceling) return;
    run.canceling = true;
    run.text = "Stopping…";
    run.cancel();
    render();
  }

  /** Stop a run this tab didn't start: no goal handle to cancel, so ask the
   *  skills server to cancel whatever is running. */
  function stopExternRun() {
    if (externCanceling) return;
    externCanceling = true;
    render();
    rosClient.callService(CANCEL_SKILL_SERVICE).then(
      (res) => {
        // success=false means nothing was running (already over) — the status
        // topic clears the readout; just re-arm the button.
        if (res?.success === false) {
          externCanceling = false;
          render();
        }
      },
      () => {
        externCanceling = false;
        render();
      },
    );
  }

  // ---- open / close -------------------------------------------------------

  /** @param {boolean} next */
  function setOpen(next) {
    open = next;
    menu.classList.toggle("open", open);
    btn.classList.toggle("active", open);
    if (open) render();
  }

  /** @param {MouseEvent} e */
  function onDocClick(e) {
    if (open && !menu.contains(/** @type {Node} */ (e.target))) setOpen(false);
  }
  /** @param {KeyboardEvent} e */
  function onKeydown(e) {
    if (e.key === "Escape" && open) setOpen(false);
  }

  btn.addEventListener("click", () => setOpen(!open));
  // Keep clicks inside the menu from reaching onDocClick. Rendering a row swaps
  // the clicked element out of the DOM mid-event, so a bubbled document handler
  // would see the (now-detached) target as "outside" and wrongly close the popup.
  menu.addEventListener("click", (e) => e.stopPropagation());
  document.addEventListener("click", onDocClick);
  document.addEventListener("keydown", onKeydown);

  // ---- active-skill readout (button) --------------------------------------

  /** Name of the skill running right now, from a local run or the brain topic. */
  function currentActiveName() {
    if (run && !run.done) {
      const skillId = run.skillId; // capture: `run` is mutable, so the closure can't re-narrow it
      const s = skills.find((x) => x.id === skillId);
      return s ? formatName(s) : prettify(skillId);
    }
    return topicActiveName;
  }

  /** Paint the button's dot (grey/green) + active-skill label. */
  function syncActive() {
    const name = currentActiveName();
    const on = name !== "";
    btnDot.classList.toggle("on", on);
    btnActive.classList.toggle("on", on);
    btnActive.textContent = on ? name : "None active";
    btnActive.title = on ? name : "";
  }

  // ---- rendering ----------------------------------------------------------

  function render() {
    syncActive();
    const frag = document.createDocumentFragment();
    // A run started elsewhere (agent, CLI, another tab) has no local cancel
    // handle — offer a Stop that goes through /brain/cancel_skill instead.
    if (topicActiveName && !(run && !run.done)) frag.appendChild(renderExternRow());
    for (const skill of skills) frag.appendChild(renderRow(skill));
    if (skills.length === 0) {
      const empty = document.createElement("p");
      empty.className = "skills-pop-empty";
      empty.textContent = rosClient.state === "connected" ? "No skills available." : "Not connected.";
      frag.appendChild(empty);
    }
    listEl.replaceChildren(frag);
  }

  /** Banner row for the externally-started run: name + Stop. */
  function renderExternRow() {
    const row = document.createElement("div");
    row.className = "skills-pop-row";
    const status = document.createElement("div");
    status.className = "skills-pop-status";
    const txt = document.createElement("span");
    txt.textContent = `${topicActiveName} — running`;
    const stop = document.createElement("button");
    stop.type = "button";
    stop.className = "skill-confirm stop";
    stop.textContent = externCanceling ? "Stopping" : "Stop";
    stop.disabled = externCanceling || rosClient.state !== "connected";
    stop.addEventListener("click", stopExternRun);
    status.append(txt, stop);
    row.appendChild(status);
    return row;
  }

  /** @param {any} skill */
  function renderRow(skill) {
    const expandable = hasParams(skill);
    const isExpanded = expandable && expandedId === skill.id;
    const running = !!run && run.skillId === skill.id && !run.done;
    const done = !!run && run.skillId === skill.id && run.done;

    const row = document.createElement("div");
    row.className = "skills-pop-row" + (isExpanded ? " expanded" : "");

    const head = document.createElement("button");
    head.type = "button";
    head.className = "skills-pop-item";
    head.disabled = rosClient.state !== "connected";

    const typeMeta = skillTypeMeta(skill);
    const dot = document.createElement("span");
    dot.className = "skills-pop-type-dot" + (typeMeta ? ` ${typeMeta.cls}` : "");
    if (typeMeta) dot.title = typeMeta.label;

    const name = document.createElement("span");
    name.className = "skills-pop-name";
    name.textContent = formatName(skill);
    const guidelines = skill.guidelines || skill.guidelines_when_running;
    if (guidelines) head.title = guidelines;

    const tail = document.createElement("span");
    tail.className = "skills-pop-tail mono";
    if (running) tail.textContent = "…";
    else if (expandable) tail.textContent = isExpanded ? "▾" : "›";
    head.append(dot, name, tail);

    head.addEventListener("click", () => {
      if (expandable) {
        expandedId = isExpanded ? null : skill.id;
        render();
        const expandedRow = listEl.querySelector(".skills-pop-row.expanded");
        if (!expandedRow) return;
        const expandedRowOverflow = expandedRow.getBoundingClientRect().bottom - scrollEl.getBoundingClientRect().bottom;
        if (expandedRowOverflow > 0) scrollEl.scrollTop += expandedRowOverflow;
        return;
      }
      // Same ANY-run guard as the form Run button/Enter key: launching while
      // another skill runs would overwrite the single `run` slot and discard
      // the running skill's cancel handle — leaving no UI path to stop it.
      if (run && !run.done) return;
      if (rosClient.state !== "connected") return;
      startRun(skill);
    });

    row.appendChild(head);

    // Inline status for a parameter-less skill (no form to host it).
    if (!expandable && (running || done)) {
      const status = document.createElement("div");
      status.className = "skills-pop-status" + (run && run.error ? " error" : "");
      const txt = document.createElement("span");
      txt.textContent = run ? run.text : "";
      status.appendChild(txt);
      if (running) {
        const stop = document.createElement("button");
        stop.type = "button";
        stop.className = "skill-confirm stop";
        stop.textContent = run?.canceling ? "Stopping" : "Stop";
        stop.disabled = !!run?.canceling;
        stop.addEventListener("click", stopRun);
        status.appendChild(stop);
      }
      row.appendChild(status);
    }

    if (isExpanded) row.appendChild(renderForm(skill, running));
    return row;
  }

  /** @param {any} skill @param {boolean} running */
  function renderForm(skill, running) {
    const form = document.createElement("div");
    form.className = "skill-form";

    const schema = getSkillInputs(skill);
    for (const [paramName, spec] of Object.entries(schema)) {
      form.appendChild(renderParam(skill, paramName, spec));
    }

    const footer = document.createElement("div");
    footer.className = "skill-form-footer";

    const status = document.createElement("p");
    status.className = "skill-status" + (run && run.skillId === skill.id && run.error ? " error" : "");
    status.textContent = run && run.skillId === skill.id ? run.text : "";

    const action = document.createElement("button");
    action.type = "button";
    if (running) {
      action.className = "skill-confirm stop";
      action.textContent = run?.canceling ? "Stopping" : "Stop";
      action.disabled = !!run?.canceling;
      action.addEventListener("click", stopRun);
    } else {
      action.className = "skill-confirm";
      action.textContent = "Run";
      const otherRunning = run && !run.done && run.skillId !== skill.id;
      action.disabled = !!otherRunning || rosClient.state !== "connected";
      action.addEventListener("click", () => startRun(skill));
    }

    footer.append(status, action);
    form.appendChild(footer);
    return form;
  }

  /** @param {any} skill @param {string} paramName @param {any} spec */
  function renderParam(skill, paramName, spec) {
    const t = schemaType(spec);
    const options = enumValues(spec);
    const value = valueFor(skill.id, paramName, spec);

    const rowEl = document.createElement("label");
    rowEl.className = "skill-param";

    const labelRow = document.createElement("span");
    labelRow.className = "skill-param-label";
    const pn = document.createElement("span");
    pn.textContent = paramName + (isRequired(spec) ? " *" : "");
    const pt = document.createElement("span");
    pt.className = "skill-param-type mono";
    pt.textContent = typeof spec === "string" ? spec : spec?.type ?? "any";
    labelRow.append(pn, pt);
    rowEl.appendChild(labelRow);

    if (options.length > 0) {
      const sel = document.createElement("select");
      sel.className = "skill-input mono";
      if (!isRequired(spec)) sel.appendChild(new Option("— unset —", ""));
      for (const opt of options) {
        const o = new Option(String(opt), String(opt));
        if (String(opt) === value) o.selected = true;
        sel.appendChild(o);
      }
      sel.value = value;
      sel.addEventListener("change", () => setValue(skill.id, paramName, sel.value));
      rowEl.appendChild(sel);
    } else if (isBool(t)) {
      const group = document.createElement("div");
      group.className = "skill-bool";
      for (const label of ["true", "false"]) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "skill-bool-opt" + (value === label ? " on" : "");
        b.textContent = label;
        b.addEventListener("click", () => {
          setValue(skill.id, paramName, label);
          render();
        });
        group.appendChild(b);
      }
      rowEl.appendChild(group);
    } else if (isJson(t)) {
      const ta = document.createElement("textarea");
      ta.className = "skill-input skill-textarea mono";
      ta.value = value;
      ta.placeholder = `${paramName}…`;
      ta.addEventListener("input", () => setValue(skill.id, paramName, ta.value));
      rowEl.appendChild(ta);
    } else {
      const inp = document.createElement("input");
      inp.className = "skill-input mono";
      // type=text (not number) so a numeric field's decimal separator always
      // renders as a dot regardless of browser/OS locale; inputmode keeps the
      // mobile numpad. buildInputs() normalizes any comma the user still types.
      inp.type = "text";
      if (isNumeric(t)) inp.inputMode = "decimal";
      inp.value = value;
      inp.placeholder = `${paramName}…`;
      inp.addEventListener("input", () => setValue(skill.id, paramName, inp.value));
      // Enter submits the skill (textarea is left alone so JSON can have newlines).
      inp.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !(run && !run.done)) {
          e.preventDefault();
          startRun(skill);
        }
      });
      rowEl.appendChild(inp);
    }
    return rowEl;
  }

  // ---- live data ----------------------------------------------------------

  const unsubSkills = rosClient.subscribe(AVAILABLE_SKILLS_TOPIC, (msg) => {
    /** @type {any[]} */
    const all = Array.isArray(msg?.skills) ? msg.skills : [];
    // Pinned skills float to the top in PINNED_SKILLS order; the rest keep their
    // roster order (mirrors the sim console's sortSkills). Still-training skills
    // aren't runnable yet, so they're dropped rather than shown disabled.
    const next = all
      .filter((s) => s && s.id && !s.in_training)
      .map((s, index) => ({ s, index }))
      .sort((a, b) => pinnedRank(a.s) - pinnedRank(b.s) || a.index - b.index)
      .map((entry) => entry.s);
    // The roster is latched and republishes on any change; avoid a re-render
    // (which would steal focus mid-typing) unless the set actually changed.
    const sig = next.map((s) => s.id).join("|");
    if (sig === signature) return;
    signature = sig;
    skills = next;
    if (expandedId && !skills.some((s) => s.id === expandedId)) expandedId = null;
    if (open) render();
  }, undefined, "brain_messages/msg/AvailableSkills");

  // The brain announces every skill run (manual or agent-driven) here. The robot
  // runs one skill at a time, so any terminal status clears the readout.
  const unsubStatus = rosClient.subscribe(SKILL_STATUS_UPDATE_TOPIC, (m) => {
    if (typeof m?.data !== "string") return;
    let payload;
    try {
      payload = JSON.parse(m.data);
    } catch {
      return;
    }
    const name = String(payload?.primitive_name ?? payload?.skill_name ?? payload?.skill_id ?? "");
    const status = String(payload?.status ?? "");
    if (!name || !status) return;
    const prevActive = topicActiveName;
    topicActiveName = status === "running" ? prettify(name) : "";
    if (topicActiveName === "") externCanceling = false;
    syncActive();
    // The extern-run banner tracks this topic; repaint it while the popup is up.
    if (open && topicActiveName !== prevActive) render();
  }, undefined, "std_msgs/msg/String");

  const unsubState = rosClient.onStateChange(() => {
    if (rosClient.state !== "connected") {
      topicActiveName = "";
      externCanceling = false;
    }
    syncActive();
    if (open) render();
  });

  syncActive();

  return {
    destroy() {
      document.removeEventListener("click", onDocClick);
      document.removeEventListener("keydown", onKeydown);
      unsubSkills();
      unsubStatus();
      unsubState();
      if (run && !run.done) run.cancel();
      menu.remove();
    },
  };
}

/** "navigate_to_position" → "Navigate To Position". @param {string} id */
function prettify(id) {
  return String(id)
    .split("/")
    .map((part) => part.replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()))
    .join(" / ");
}

/**
 * Position in PINNED_SKILLS (or the end if unpinned), matched on the last path
 * segment with "_"→" ", lowercased — the same basis the sim console uses.
 * @param {any} skill
 */
function pinnedRank(skill) {
  const segments = String(skill?.name || skill?.id || "").split("/");
  const name = (segments[segments.length - 1] || "").replace(/_/g, " ").trim().toLowerCase();
  const i = PINNED_SKILLS.findIndex((entry) => entry.trim().toLowerCase() === name);
  return i === -1 ? PINNED_SKILLS.length : i;
}

/** "pick_and_place/cup" → "Pick And Place / Cup". @param {any} skill */
function formatName(skill) {
  if (skill?.name) return skill.name;
  return String(skill?.id ?? "")
    .split("/")
    .map((part) => part.replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()))
    .join(" / ");
}

/**
 * Maps a skill's roster `type` ("code" | "learned" | "replay" | "poses") to the
 * dot color + tooltip shown before its name. Unknown/missing type renders no dot.
 * @param {any} skill
 * @returns {{ cls: string, label: string } | null}
 */
function skillTypeMeta(skill) {
  switch (skill?.type) {
    case "code":
      return { cls: "digital", label: "Digital skill" };
    case "replay":
      return { cls: "replay", label: "Replay skill" };
    case "learned":
    case "poses":
      return { cls: "learned", label: "Learned skill" };
    default:
      return null;
  }
}

/** Static key for the type dots, pinned above the (scrollable) skill list. */
function buildTypeLegend() {
  const legend = document.createElement("div");
  legend.className = "skills-pop-legend";
  for (const { cls, label } of [
    { cls: "learned", label: "Learned" },
    { cls: "replay", label: "Replay" },
    { cls: "digital", label: "Digital" },
  ]) {
    const item = document.createElement("span");
    item.className = "skills-pop-legend-item";
    const dot = document.createElement("span");
    dot.className = `skills-pop-type-dot ${cls}`;
    const text = document.createElement("span");
    text.textContent = label;
    item.append(dot, text);
    legend.appendChild(item);
  }
  return legend;
}
