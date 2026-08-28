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

/** @param {any[]} skills @param {string} query */
export function searchSkills(skills, query) {
  const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  return skills.filter((skill) => {
    const text = `${formatName(skill)} ${skill.id} ${skill.group ?? ""}`.toLowerCase();
    return terms.every((term) => text.includes(term));
  });
}

export const nextSkillIndex = (index, length, delta) =>
  Math.max(0, Math.min(length - 1, index + delta));

// onKeydown accepts both ⌘ and Ctrl chords; these only pick what to advertise.
const isMac = /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent);
const shortcutLabel = isMac ? "⌘K" : "Ctrl+K";
const stopShortcutLabel = isMac ? "⌘I" : "Ctrl+I";

/** @param {string} label */
function shortcutKbd(label) {
  const kbd = document.createElement("kbd");
  kbd.className = "skill-run-kbd mono";
  kbd.textContent = label;
  return kbd;
}

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
  btn.title = `Run a skill (${shortcutLabel} or Space) — roster from ${AVAILABLE_SKILLS_TOPIC}`;
  btn.setAttribute("aria-haspopup", "true");
  btn.setAttribute("aria-expanded", "false");
  const btnDot = document.createElement("span");
  btnDot.className = "skills-menu-dot";
  btnDot.title = "green while a skill is running";
  const btnLabel = document.createElement("span");
  btnLabel.className = "skills-menu-label";
  btnLabel.textContent = "Skills";
  // Currently-running skill (from /brain/skill_status_update), or "None active".
  const btnActive = document.createElement("span");
  btnActive.className = "skills-menu-active";
  btnActive.textContent = "None active";
  const btnShortcut = document.createElement("kbd");
  btnShortcut.className = "skills-menu-kbd mono";
  btnShortcut.textContent = shortcutLabel;
  const btnChevron = document.createElement("span");
  btnChevron.className = "skills-menu-chevron mono";
  btnChevron.textContent = "▾";
  btn.append(btnDot, btnLabel, btnActive, btnShortcut, btnChevron);

  const pop = document.createElement("div");
  pop.className = "skills-pop";
  const search = document.createElement("div");
  search.className = "skills-pop-search";
  const searchInput = document.createElement("input");
  searchInput.type = "search";
  searchInput.className = "skills-pop-search-input";
  searchInput.placeholder = "Search skills…";
  searchInput.autocomplete = "off";
  searchInput.spellcheck = false;
  searchInput.setAttribute("aria-label", "Search skills");
  const searchKbd = document.createElement("kbd");
  searchKbd.className = "skills-pop-search-kbd mono";
  searchKbd.textContent = shortcutLabel;
  search.append(searchInput, searchKbd);
  pop.appendChild(search);
  pop.appendChild(buildTypeLegend());
  const scrollEl = document.createElement("div");
  scrollEl.className = "skills-pop-scroll";
  const listEl = document.createElement("div");
  listEl.className = "skills-pop-list";
  scrollEl.appendChild(listEl);
  pop.appendChild(scrollEl);

  // Interrupt without opening the menu: a red pill beside the button, shown
  // only while a skill runs and the popup is closed (it has its own Stops).
  const stopBtn = document.createElement("button");
  stopBtn.type = "button";
  stopBtn.className = "skills-stop-btn";
  stopBtn.title = "Interrupt the running skill";
  stopBtn.hidden = true;
  stopBtn.addEventListener("click", () => {
    if (run && !run.done) stopRun();
    else if (topicActiveName) stopExternRun();
  });

  menu.append(pop, btn, stopBtn);
  parent.appendChild(menu);

  // ---- state --------------------------------------------------------------

  let open = false;
  /** @type {any[]} */
  let skills = [];
  let signature = "";
  /** @type {string | null} */
  let expandedId = null;
  /** @type {string | null} */
  let selectedId = null;
  /** Folder sections the user expanded (by group path) — folders start closed. @type {Set<string>} */
  const expandedGroups = new Set();
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
    // The launch re-render dropped focus to the body, where keys mean driving
    // — keep the keyboard in skills control, ready for the next pick.
    if (open) {
      searchInput.focus();
      searchInput.select();
    }

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
    btn.setAttribute("aria-expanded", String(open));
    syncActive();
    if (open) {
      render();
      requestAnimationFrame(() => {
        if (!open) return;
        searchInput.focus();
        searchInput.select();
      });
    }
  }

  /** @param {MouseEvent} e */
  function onDocClick(e) {
    if (open && !menu.contains(/** @type {Node} */ (e.target))) setOpen(false);
  }
  /** @param {KeyboardEvent} e */
  function onKeydown(e) {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      setOpen(!open);
      if (!open) btn.focus();
    } else if ((e.metaKey || e.ctrlKey) && (e.key.toLowerCase() === "i" || e.key === ".")) {
      // ⌘I (plus ⌘., the platform cancel chord, as a silent alias) stops
      // whatever is running — popup closed too, agent-started runs included.
      e.preventDefault();
      if (run && !run.done) stopRun();
      else if (topicActiveName) stopExternRun();
    } else if (e.key === "Escape" && open) {
      // Staged: first Escape backs out of an expanded form to the list,
      // the next closes the popup.
      if (expandedId) {
        expandedId = null;
        render();
        searchInput.focus();
        searchInput.select();
      } else {
        setOpen(false);
        btn.focus();
      }
    } else if (e.key === "Enter" && open && expandedId && enterRunsForm(e.target)) {
      e.preventDefault();
      if (run && !run.done) {
        if (run.skillId === expandedId) stopRun(); // the form's action button is Stop
        return;
      }
      if (rosClient.state !== "connected") return;
      const skill = skills.find((s) => s.id === expandedId);
      if (skill) startRun(skill);
    } else if (e.key === " " && !e.repeat && !e.metaKey && !e.ctrlKey && !e.altKey && neutralFocus(e.target)) {
      e.preventDefault();
      if (!open) setOpen(true);
      else toggleSelectionDropdown();
    } else if (open && !e.metaKey && !e.ctrlKey && !e.altKey && /^[0-9]$/.test(e.key) && digitPicksSkill(e.target)) {
      e.preventDefault();
      e.stopPropagation(); // the shell binds bare digits to page navigation
      // 1-9 pick the first nine rows; 0 rounds out the row of keys as the tenth.
      const button = selectableButtons()[e.key === "0" ? 9 : Number(e.key) - 1];
      if (button) {
        selectButton(button);
        activateSelection();
      }
    }
  }

  /** Digits pick a numbered row only where they can't mean typed text: the
   *  empty search box, or body focus after a re-render. A param field keeps
   *  digits for values, a live query keeps them for the search.
   *  @param {EventTarget | null} target */
  function digitPicksSkill(target) {
    if (target === searchInput) return searchInput.value === "";
    return neutralFocus(target);
  }

  /** Body (or nothing) focused — a key here can't mean typing or a native
   *  button activation. @param {EventTarget | null} target */
  function neutralFocus(target) {
    return !(target instanceof HTMLElement) || target === document.body;
  }

  /** Enter runs the expanded form unless focus is somewhere Enter already has a
   *  job: the search box (activates the selection), a textarea (JSON newlines),
   *  a button/select (native activation), or an input elsewhere on the page.
   *  Body counts — expanding a row re-renders and drops focus there.
   *  @param {EventTarget | null} target */
  function enterRunsForm(target) {
    if (!(target instanceof HTMLElement) || target === document.body) return true;
    if (!menu.contains(target) || target === searchInput) return false;
    return target instanceof HTMLInputElement;
  }

  btn.addEventListener("click", () => setOpen(!open));
  searchInput.addEventListener("input", () => {
    selectedId = null;
    render();
  });
  searchInput.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      moveSelection(e.key === "ArrowDown" ? 1 : -1);
    } else if (e.key === "Enter" && !e.isComposing) {
      e.preventDefault();
      activateSelection();
    } else if (e.key === " " && searchInput.value === "") {
      // A space can't start a query, so it opens the selected row's dropdown.
      e.preventDefault();
      toggleSelectionDropdown();
    }
  });
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

  /** Paint the button's dot (grey/green) + active-skill label + bar Stop. */
  function syncActive() {
    const name = currentActiveName();
    const on = name !== "";
    btnDot.classList.toggle("on", on);
    btnActive.classList.toggle("on", on);
    btnActive.textContent = on ? name : "None active";
    btnActive.title = on ? name : "";
    const localRunning = !!run && !run.done;
    const canceling = (localRunning && run.canceling) || externCanceling;
    stopBtn.hidden = !on || open;
    stopBtn.disabled = canceling || (!localRunning && rosClient.state !== "connected");
    if (canceling) stopBtn.textContent = "Stopping";
    else stopBtn.replaceChildren("Stop", shortcutKbd(stopShortcutLabel));
  }

  // ---- rendering ----------------------------------------------------------

  function render() {
    syncActive();
    const frag = document.createDocumentFragment();
    const query = searchInput.value.trim();
    const visibleSkills = searchSkills(skills, query);
    // A run started elsewhere (agent, CLI, another tab) has no local cancel
    // handle — offer a Stop that goes through /brain/cancel_skill instead.
    if (topicActiveName && !(run && !run.done)) frag.appendChild(renderExternRow());
    // Root skills flat first (pinned order preserved), then one collapsible
    // section per folder (SkillInfo.group), folders alphabetical.
    for (const skill of visibleSkills) {
      if (!skill.group) frag.appendChild(renderRow(skill));
    }
    for (const [group, members] of groupedSkills(visibleSkills)) {
      frag.appendChild(renderGroupHeader(group, members.length));
      if (query || expandedGroups.has(group)) {
        for (const skill of members) frag.appendChild(renderRow(skill));
      }
    }
    if (visibleSkills.length === 0) {
      const empty = document.createElement("p");
      empty.className = "skills-pop-empty";
      empty.textContent = query
        ? `No skills match “${query}”.`
        : rosClient.state === "connected" ? "No skills available." : "Not connected.";
      frag.appendChild(empty);
    }
    listEl.replaceChildren(frag);
    const buttons = selectableButtons();
    if (!buttons.some((button) => button.dataset.skillId === selectedId)) {
      selectedId = null;
      if (buttons[0]) selectButton(buttons[0]);
    }
    // Number hints mirror digitPicksSkill: digits only work while the search
    // is empty, so the badges only show then — a filtered list never lies.
    // Every row keeps its (blank) number slot so the columns stay aligned.
    if (!query) {
      for (const [i, button] of buttons.slice(0, 10).entries()) {
        const num = button.querySelector(".skills-pop-num");
        if (num) num.textContent = i === 9 ? "0" : String(i + 1);
      }
    }
  }

  function selectableButtons() {
    return [...listEl.querySelectorAll(".skills-pop-item:not(:disabled)")].map((el) => /** @type {HTMLButtonElement} */ (el));
  }

  /** @param {HTMLButtonElement} button */
  function selectButton(button) {
    listEl.querySelector(".skills-pop-item.selected")?.classList.remove("selected");
    button.classList.add("selected");
    selectedId = button.dataset.skillId ?? null;
    button.scrollIntoView({ block: "nearest" });
  }

  function moveSelection(delta) {
    const buttons = selectableButtons();
    const index = buttons.findIndex((button) => button.dataset.skillId === selectedId);
    const next = nextSkillIndex(index, buttons.length, delta);
    if (buttons[next]) selectButton(buttons[next]);
  }

  /** Move focus between a form's controls (fields in order, Run last).
   *  @param {HTMLElement} fromEl @param {number} delta */
  function focusFormControl(fromEl, delta) {
    const form = fromEl.closest(".skill-form");
    if (!form) return;
    const controls = [...form.querySelectorAll(".skill-input, .skill-choice, .skill-bool, .skill-confirm")];
    const next = controls[controls.indexOf(fromEl) + delta];
    if (next instanceof HTMLElement) next.focus();
  }

  /** Up/Down walk the form, Space advances, Enter runs — shared by every
   *  single-tab-stop group widget (enum pills, bool). Left/Right stay the
   *  group's own: they choose within it via `move`.
   *  @param {HTMLElement} group @param {any} skill @param {(delta: number) => void} move */
  function wireGroupKeys(group, skill, move) {
    group.addEventListener("keydown", (e) => {
      if (e.key === "ArrowRight") {
        e.preventDefault();
        move(1);
      } else if (e.key === "ArrowLeft") {
        e.preventDefault();
        move(-1);
      } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        focusFormControl(group, e.key === "ArrowDown" ? 1 : -1);
      } else if (e.key === " ") {
        e.preventDefault();
        focusFormControl(group, 1);
      } else if (e.key === "Enter" && !(run && !run.done)) {
        e.preventDefault();
        startRun(skill);
      }
    });
  }

  /** Space on a selected row: toggle its param dropdown (focus lands on the
   *  first field). Never launches — Enter and click are the run gestures. */
  function toggleSelectionDropdown() {
    const button = /** @type {HTMLButtonElement | null} */ (listEl.querySelector(".skills-pop-item.selected"));
    const skill = skills.find((s) => s.id === button?.dataset.skillId);
    if (!button || !skill || !hasParams(skill)) return;
    button.click();
    requestAnimationFrame(() => {
      const input = listEl.querySelector(".skills-pop-row.expanded :is(.skill-input, .skill-choice, .skill-bool)");
      if (input instanceof HTMLElement) input.focus();
      else searchInput.focus();
    });
  }

  function activateSelection() {
    const button = /** @type {HTMLButtonElement | null} */ (listEl.querySelector(".skills-pop-item.selected"));
    if (!button) return;
    button.click();
    requestAnimationFrame(() => {
      const input = listEl.querySelector(".skills-pop-row.expanded :is(.skill-input, .skill-choice, .skill-bool)");
      if (input instanceof HTMLElement) input.focus();
    });
  }

  /** Grouped skills as [group, members][] with folders alphabetical; members
   *  keep the pinned/roster order of `skills`.
   *  @param {any[]} visibleSkills @returns {[string, any[]][]} */
  function groupedSkills(visibleSkills) {
    /** @type {Map<string, any[]>} */
    const groups = new Map();
    for (const skill of visibleSkills) {
      const group = typeof skill.group === "string" ? skill.group : "";
      if (!group) continue;
      const members = groups.get(group) ?? [];
      members.push(skill);
      groups.set(group, members);
    }
    return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b));
  }

  /** Section header for one folder: click toggles collapse. @param {string} group @param {number} count */
  function renderGroupHeader(group, count) {
    const collapsed = !expandedGroups.has(group);
    const head = document.createElement("button");
    head.type = "button";
    head.className = "skills-pop-group";
    head.title = "Collapse/expand this skill folder";
    const name = document.createElement("span");
    name.className = "skills-pop-group-name";
    name.textContent = prettify(group);
    const tail = document.createElement("span");
    tail.className = "skills-pop-tail mono";
    tail.textContent = collapsed ? "›" : "▾";
    head.append(name, tail);
    if (collapsed) {
      // Count beside the name, not in the tail — the right edge is reserved
      // for shortcut digits and chevrons, and a bare count there mimics them.
      const countEl = document.createElement("span");
      countEl.className = "skills-pop-group-count mono";
      countEl.textContent = String(count);
      name.after(countEl);
    }
    head.addEventListener("click", () => {
      if (collapsed) expandedGroups.add(group);
      else expandedGroups.delete(group);
      render();
    });
    return head;
  }

  /** Banner row for the externally-started run: name + Stop. */
  function renderExternRow() {
    const row = document.createElement("div");
    row.className = "skills-pop-row expanded";
    const status = document.createElement("div");
    status.className = "skills-pop-status";
    const txt = document.createElement("span");
    txt.textContent = `${topicActiveName} — running`;
    const stop = document.createElement("button");
    stop.type = "button";
    stop.className = "skill-confirm stop compact";
    if (externCanceling) stop.textContent = "Stopping";
    else stop.append("Stop", shortcutKbd(stopShortcutLabel));
    stop.title = `this run was started elsewhere (agent or another client) — ${CANCEL_SKILL_SERVICE}`;
    stop.disabled = externCanceling || rosClient.state !== "connected";
    stop.addEventListener("click", stopExternRun);
    status.append(txt, stop);
    row.appendChild(status);
    return row;
  }

  /** A skill whose file failed to load: not runnable, shows the error instead. @param {any} skill */
  function renderBrokenRow(skill) {
    const row = document.createElement("div");
    row.className = "skills-pop-row";
    const head = document.createElement("button");
    head.type = "button";
    head.className = "skills-pop-item";
    head.disabled = true;
    head.title = skill.load_error;
    const dot = document.createElement("span");
    dot.className = "skills-pop-type-dot broken";
    dot.title = "Failed to load";
    const name = document.createElement("span");
    name.className = "skills-pop-name";
    name.textContent = formatName(skill);
    head.append(dot, name);
    const status = document.createElement("div");
    status.className = "skills-pop-status error";
    const txt = document.createElement("span");
    txt.textContent = skill.load_error;
    status.appendChild(txt);
    row.append(head, status);
    return row;
  }

  /** @param {any} skill */
  function renderRow(skill) {
    if (skill.load_error) return renderBrokenRow(skill);
    const expandable = hasParams(skill);
    const isExpanded = expandable && expandedId === skill.id;
    const running = !!run && run.skillId === skill.id && !run.done;
    const done = !!run && run.skillId === skill.id && run.done;

    const row = document.createElement("div");
    // The card backdrop groups a head with whatever renders beneath it — a
    // param form, or a parameter-less skill's status line and Stop.
    const carded = isExpanded || (!expandable && (running || done));
    row.className = "skills-pop-row" + (carded ? " expanded" : "");

    const head = document.createElement("button");
    head.type = "button";
    head.className = "skills-pop-item" + (selectedId === skill.id ? " selected" : "");
    head.dataset.skillId = skill.id;
    head.disabled = rosClient.state !== "connected";

    const num = document.createElement("span");
    num.className = "skills-pop-num mono";
    const typeMeta = skillTypeMeta(skill);
    const dot = document.createElement("span");
    dot.className = "skills-pop-type-dot" + (typeMeta ? ` ${typeMeta.cls}` : "");
    if (typeMeta) dot.title = typeMeta.label;

    const name = document.createElement("span");
    name.className = "skills-pop-name";
    name.textContent = formatName(skill);
    const guidelines = skill.guidelines || skill.guidelines_when_running;
    head.title = guidelines || `run ${skill.id} — ${EXECUTE_SKILL_ACTION}`;

    const tail = document.createElement("span");
    tail.className = "skills-pop-tail mono";
    if (running) tail.textContent = "…";
    else if (expandable) tail.textContent = isExpanded ? "▾" : "›";
    head.append(dot, name, num, tail);

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
    head.addEventListener("mouseenter", () => selectButton(head));

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
        stop.className = "skill-confirm stop compact";
        if (run?.canceling) stop.textContent = "Stopping";
        else stop.append("Stop", shortcutKbd(stopShortcutLabel));
        stop.disabled = !!run?.canceling;
        stop.addEventListener("click", stopRun);
        status.appendChild(stop);
      } else {
        // Done: clicking the head reruns, but nothing says so — give the
        // card the same Run action a form footer has.
        const again = document.createElement("button");
        again.type = "button";
        again.className = "skill-confirm compact";
        again.append("Run", shortcutKbd("↵"));
        again.title = EXECUTE_SKILL_ACTION;
        again.disabled = rosClient.state !== "connected";
        again.addEventListener("click", () => {
          if (!(run && !run.done)) startRun(skill);
        });
        status.appendChild(again);
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
      if (run?.canceling) action.textContent = "Stopping";
      else action.append("Stop", shortcutKbd(stopShortcutLabel));
      action.disabled = !!run?.canceling;
      action.addEventListener("click", stopRun);
    } else {
      action.className = "skill-confirm";
      action.append("Run", shortcutKbd("↵"));
      action.title = `${EXECUTE_SKILL_ACTION} — Enter runs`;
      const otherRunning = run && !run.done && run.skillId !== skill.id;
      action.disabled = !!otherRunning || rosClient.state !== "connected";
      action.addEventListener("click", () => startRun(skill));
    }

    action.addEventListener("keydown", (e) => {
      if (e.key === "ArrowUp") {
        e.preventDefault();
        focusFormControl(action, -1);
      }
    });

    footer.append(status, action);
    form.appendChild(footer);
    return form;
  }

  /** Enum param as a keyboard-first pill group: the group is one focusable
   *  widget, arrows cycle the value in place (no re-render, so focus holds),
   *  Enter runs. An optional param gets a leading "—" pill meaning unset.
   *  @param {any} skill @param {string} paramName @param {any} spec @param {any[]} options */
  function renderChoice(skill, paramName, spec, options) {
    const labels = (isRequired(spec) ? [] : [""]).concat(options.map(String));
    const group = document.createElement("div");
    group.className = "skill-choice";
    group.tabIndex = 0;
    group.setAttribute("role", "radiogroup");
    group.setAttribute("aria-label", paramName);
    const value = valueFor(skill.id, paramName, spec);
    const pills = labels.map((label) => {
      const pill = document.createElement("button");
      pill.type = "button";
      pill.tabIndex = -1; // the group is the tab stop; arrows move within it
      pill.className = "skill-choice-opt" + (label === value ? " on" : "");
      pill.textContent = label === "" ? "—" : label;
      if (label === "") pill.title = "unset";
      pill.addEventListener("click", () => {
        select(label);
        group.focus();
      });
      return pill;
    });
    group.append(...pills);

    /** @param {string} label */
    function select(label) {
      setValue(skill.id, paramName, label);
      for (const [i, pill] of pills.entries()) pill.classList.toggle("on", labels[i] === label);
    }
    /** @param {number} delta */
    function move(delta) {
      const current = labels.indexOf(valueFor(skill.id, paramName, spec));
      if (current === -1) return select(labels[delta > 0 ? 0 : labels.length - 1]);
      select(labels[(current + delta + labels.length) % labels.length]);
    }
    wireGroupKeys(group, skill, move);
    return group;
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
    if (isRequired(spec)) pn.title = "Required";
    const pt = document.createElement("span");
    pt.className = "skill-param-type mono";
    pt.textContent = typeof spec === "string" ? spec : spec?.type ?? "any";
    labelRow.append(pn, pt);
    rowEl.appendChild(labelRow);

    if (options.length > 0) {
      rowEl.appendChild(renderChoice(skill, paramName, spec, options));
    } else if (isBool(t)) {
      const group = document.createElement("div");
      group.className = "skill-bool";
      group.tabIndex = 0;
      group.setAttribute("role", "radiogroup");
      group.setAttribute("aria-label", paramName);
      const labels = ["true", "false"];
      const opts = labels.map((label) => {
        const b = document.createElement("button");
        b.type = "button";
        b.tabIndex = -1; // the group is the tab stop; arrows move within it
        b.className = "skill-bool-opt" + (value === label ? " on" : "");
        b.textContent = label;
        b.addEventListener("click", () => {
          select(label);
          group.focus();
        });
        return b;
      });
      group.append(...opts);
      /** @param {string} label */
      function select(label) {
        setValue(skill.id, paramName, label);
        for (const [i, b] of opts.entries()) b.classList.toggle("on", labels[i] === label);
      }
      wireGroupKeys(group, skill, (delta) => {
        const current = labels.indexOf(valueFor(skill.id, paramName, spec));
        if (current === -1) return select(labels[delta > 0 ? 0 : labels.length - 1]);
        select(labels[(current + delta + labels.length) % labels.length]);
      });
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
      // Vertical arrows walk the form; Left/Right stay caret movement.
      inp.addEventListener("keydown", (e) => {
        if (e.key === "ArrowDown" || e.key === "ArrowUp") {
          e.preventDefault();
          focusFormControl(inp, e.key === "ArrowDown" ? 1 : -1);
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
    // load_error and group are part of the signature: a skill breaking (or
    // moving folders) must repaint even though the id set is identical.
    const sig = next.map((s) => s.id + ":" + (s.group || "") + (s.load_error ? `!${s.load_error}` : "")).join("|");
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
  for (const { cls, label, hint } of [
    { cls: "learned", label: "Learned", hint: "Trained from demonstrations (ACT policy)" },
    { cls: "replay", label: "Replay", hint: "Recorded motion played back" },
    { cls: "digital", label: "Digital", hint: "Python skill" },
  ]) {
    const item = document.createElement("span");
    item.className = "skills-pop-legend-item";
    item.title = hint;
    const dot = document.createElement("span");
    dot.className = `skills-pop-type-dot ${cls}`;
    const text = document.createElement("span");
    text.textContent = label;
    item.append(dot, text);
    legend.appendChild(item);
  }
  return legend;
}
