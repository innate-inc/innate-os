// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Settings page — a guided editor over config/settings.yaml. The catalog (knobs,
// defaults, docs) lives in catalog.js; this renders a row per knob showing its
// default and current value, lets you override or reset, and saves via the
// proxy (which edits settings.yaml surgically, so hand comment/uncomment over
// SSH keeps working). Doesn't use rosbridge — it talks to the proxy directly:
// GET /settings.json to read, POST /settings.json to write.
//
// Per-row state: a *saved* override (non-default, persisted) shows orange; an
// *unsaved* edit (differs from what's saved) shows blue. Save is enabled only
// while there are unsaved changes.

import { ROBOT_INFO_TOPIC, SET_VOLUME_SERVICE, SHUTDOWN_SERVICE } from "../constants.js";
import { ros } from "../rosClient.js";
import { SETTINGS_PAGES } from "./catalog.js";
import { GROUP_EXPAND_MS, SETTINGS_STYLE } from "./styles.js";

// Assigned per mount by mount() at the bottom. The volume control uses the shared
// rosbridge socket, which the router connects once at boot and keeps up across
// navigation, so this page no longer manages its own connection.
/** @type {HTMLElement} */
let stage;
/** @type {HTMLStyleElement | null} */
let styleEl = null;
/** Per-mount teardowns: ros subscriptions to drop when we navigate away. */
/** @type {(() => void)[]} */
let cleanups = [];

// A reconnect faster than this after a shutdown-time drop means the request
// was lost (a real power cycle takes about a minute); slower means the robot
// was powered back on — see the reconnect watch in onShutdown().
const SHUTDOWN_QUICK_RECONNECT_MS = 15_000;

/**
 * @typedef {Object} Entry
 * @property {import("./catalog.js").Knob} knob
 * @property {boolean} overridden   Current form state: is this knob overridden?
 * @property {*} value Current form value.
 * @property {boolean} savedOverridden  Last-saved (on-disk) state.
 * @property {*} savedValue
 * @property {() => void} render  Push value + overridden state into the DOM control.
 * @property {(list: {value: string, label: string}[]) => void} [setOptions]
 *   Replace a <select>'s choices with the list read off the robot. Only on knobs the
 *   catalog gave an optionsFrom.
 * @property {HTMLElement} row
 * @property {HTMLElement} [validationEl]
 */
/** @type {Entry[]} */
const entries = [];

/**
 * @typedef {Object} GroupUI
 * @property {HTMLElement} section  The collapsible section wrapping header and body.
 * @property {HTMLElement} dot  Unsaved-changes indicator in the header.
 * @property {Entry[]} entries  Knob entries belonging to this group.
 */
/** @type {GroupUI[]} */
const groups = [];
/**
 * @typedef {Object} SearchTarget
 * @property {string} label
 * @property {string} description
 * @property {HTMLElement} row
 * @property {GroupUI} group
 * @property {string} breadcrumb
 * @property {string} visibleSearchText  Label, description, and breadcrumb.
 * @property {{label: string, text: string}[]} extraSearchSources
 * @property {string} searchText  All searchable text, lowercased.
 */
/** @type {SearchTarget[]} */
const searchTargets = [];
const MAX_SEARCH_RESULTS = 20;
/** @type {HTMLInputElement | null} */
let searchInput = null;

/** @type {HTMLButtonElement} */ let saveBtn;
/** @type {HTMLButtonElement} */ let resetAllBtn;
/** @type {HTMLButtonElement} */ let restartBtn;
/** @type {HTMLButtonElement} */ let shutdownBtn;
/** @type {HTMLElement} */ let dirtyEl;
/** @type {HTMLElement} */ let statusEl;

function getNestedValue(/** @type {any} */ obj, /** @type {string[]} */ path) {
  let current = obj;
  for (const key of path) {
    if (current == null || typeof current !== "object" || !(key in current)) return undefined;
    current = current[key];
  }
  return current;
}

function setStatus(/** @type {string} */ msg, /** @type {string} */ cls = "muted") {
  statusEl.textContent = msg;
  statusEl.className = "set-status " + cls;
}

/** @param {string} tag @param {string} className @param {string} [text] */
function textEl(tag, className, text) {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/** @param {string} type @param {string} [className] */
function inputEl(type, className = "") {
  const input = document.createElement("input");
  input.type = type;
  input.className = className;
  return input;
}

/** @param {string} className @param {string} text @param {(event: MouseEvent) => void} [onClick] */
function buttonEl(className, text, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = text;
  if (onClick) button.addEventListener("click", onClick);
  return button;
}

function buildRowText(
  /** @type {string} */ label,
  /** @type {string} */ description,
  /** @type {{href: string, text: string} | null} */ link = null,
) {
  const info = textEl("div", "set-info");
  const doc = textEl("span", "set-doc", description);
  if (link) {
    doc.append(" ");
    const anchor = textEl("a", "set-doc-link", link.text);
    anchor.setAttribute("href", link.href);
    anchor.setAttribute("target", "_blank");
    anchor.setAttribute("rel", "noopener noreferrer");
    doc.append(anchor);
  }
  info.append(textEl("span", "set-label", label), doc);
  return info;
}

function addSearchTarget(
  /** @type {Omit<SearchTarget, "visibleSearchText" | "searchText">} */ target,
) {
  const visibleSearchText = [target.breadcrumb, target.label, target.description]
    .join(" ")
    .toLowerCase();
  searchTargets.push({
    ...target,
    visibleSearchText,
    searchText: [visibleSearchText, ...target.extraSearchSources.map((source) => source.text)]
      .join(" ")
      .toLowerCase(),
  });
}

/** Always mounted; CSS shows it only when value ≠ default. */
function buildRestoreDefaultButton(/** @type {Entry} */ entry) {
  const knob = entry.knob;
  const opt = knob.options && knob.options.find((o) => o.value === knob.default);
  const defShort = opt ? opt.label : String(knob.default) + (knob.unit ? " " + knob.unit : "");
  const tip = "Restore built-in default (" + defShort + ")";
  const btn = buttonEl("set-restore", "Restore default", (event) => {
    event.stopPropagation();
    entry.overridden = false;
    entry.value = knob.default;
    entry.render();
    recompute();
  });
  btn.dataset.activate = "release";
  btn.title = tip;
  btn.setAttribute("aria-label", tip);
  return btn;
}

function coerceToKnobType(/** @type {import("./catalog.js").Knob} */ knob, /** @type {any} */ value) {
  if (knob.type === "bool") return Boolean(value);
  if (knob.type === "int" || knob.type === "float") return Number(value);
  return String(value);
}

/**
 * Why this value can't be saved, or "" if it can.
 *
 * The robot rejects out-of-range drive knobs outright (mars_app's on_set_parameters), so
 * without this the save would appear to succeed and the robot would keep the old value.
 * Bounds are the absurd limits, not tuning advice — the point is to keep a typo like
 * 0.000001 m/s² of deceleration, which takes days to stop, out of the file.
 */
function boundsError(/** @type {Entry} */ e) {
  const knob = e.knob;
  if (knob.type !== "int" && knob.type !== "float") return "";
  const v = Number(e.value);
  if (!Number.isFinite(v)) return "Not a number";
  if (knob.min !== undefined && v < knob.min) return `Min ${knob.min}`;
  if (knob.max !== undefined && v > knob.max) return `Max ${knob.max}`;
  return "";
}

/** Has this entry changed since the last save? */
function isDirty(/** @type {Entry} */ e) {
  if (e.overridden !== e.savedOverridden) return true;
  return e.overridden && e.value !== e.savedValue;
}

function setKnobValue(/** @type {Entry} */ entry, /** @type {*} */ value) {
  entry.value = value;
  entry.overridden = entry.value !== entry.knob.default;
  recompute();
}

/** Repaint every row + destination dots + the footer (dirty count, Save enabled). */
function recompute() {
  let dirty = 0;
  let bad = 0;
  for (const e of entries) {
    const d = isDirty(e);
    if (d) dirty++;
    // Only flag a knob the operator has actually touched: a bound tightened after a value
    // was already saved shouldn't light up rows nobody is editing.
    const err = d ? boundsError(e) : "";
    if (err) bad++;
    e.row.classList.toggle("dirty", d && !err);
    e.row.classList.toggle("invalid", Boolean(err));
    e.row.classList.toggle("saved", !d && e.overridden);
    // Restore tracks value≠default, not dirty/saved — otherwise flipping a toggle
    // back to default still showed Restore and clicking it felt broken.
    e.row.classList.toggle("has-override", e.value !== e.knob.default);
    if (e.validationEl) e.validationEl.textContent = err;
  }
  for (const group of groups) {
    // Headers stay quiet — the purple dot alone marks unsaved work inside a collapsed group.
    group.dot.classList.toggle("show", group.entries.some(isDirty));
  }
  dirtyEl.textContent = bad
    ? `${bad} value${bad === 1 ? "" : "s"} out of range`
    : dirty
      ? `${dirty} unsaved change${dirty === 1 ? "" : "s"}`
      : "";
  dirtyEl.classList.toggle("set-bad", bad > 0);
  saveBtn.disabled = dirty === 0 || bad > 0;
  resetAllBtn.disabled = !entries.some((e) => e.overridden);
}

/** Reset every knob to its default in the form (you still click Save to commit). */
function resetAll() {
  for (const e of entries) {
    e.overridden = false;
    e.value = e.knob.default;
    e.render();
  }
  recompute();
}

const DEFAULT_VOLUME = 80; // robot's built-in default (percent) until /robot/info arrives.

/** Clamp to an integer 0–100, mirroring the mobile app's clampVolumePercent. */
function clampVolume(/** @type {number} */ value) {
  if (!Number.isFinite(value)) return DEFAULT_VOLUME;
  return Math.max(0, Math.min(100, Math.round(value)));
}

function syncSliderFill(/** @type {HTMLInputElement} */ slider) {
  const min = Number(slider.min);
  const percent = ((Number(slider.value) - min) / (Number(slider.max) - min)) * 100;
  slider.style.setProperty("--set-slider-fill", `${percent}%`);
}

/** One-time setup: sync fill on input, track pointer so the focus ring can hide. */
function initSlider(/** @type {HTMLInputElement} */ slider) {
  syncSliderFill(slider);
  slider.addEventListener("input", () => syncSliderFill(slider));
  slider.addEventListener("pointerdown", () => slider.classList.add("is-pointer"));
  slider.addEventListener("blur", () => slider.classList.remove("is-pointer"));
}

/** Focus for row-click / search: show the ring, clearing any prior pointer grab. */
function focusControl(/** @type {HTMLElement} */ el, /** @type {FocusOptions} */ opts = {}) {
  el.classList.remove("is-pointer");
  el.focus({ focusVisible: true, ...opts });
}

/**
 * Live speaker-volume control. Unlike the yaml knobs below, this is a rosbridge
 * service call that applies immediately and persists on the robot — no restart.
 * The slider is the raw volume_percent (0–100): it reads the current value from
 * /robot/info and writes via /set_volume on release. The robot lifts the low end
 * of the range so the bottom of the slider stays audible (see apply_alsa_volume).
 * @returns {{section: HTMLElement, row: HTMLElement, label: string, description: string}}
 */
function buildVolumeSection() {
  const labelText = "Speaker volume";
  const descriptionText = "The robot's voice volume. Applies immediately — no restart.";
  const section = textEl("section", "set-card-volume");

  const row = textEl("div", "set-row");

  const controlContainer = textEl("div", "set-ctl");
  const controlGroup = textEl("div", "set-ctl-main is-wide");

  const slider = inputEl("range", "set-slider");
  slider.title = `${SET_VOLUME_SERVICE} — live value from ${ROBOT_INFO_TOPIC}`;
  slider.min = "0";
  slider.max = "100";
  slider.step = "1";
  // Resting thumb position while we wait for the robot's value; the readout shows
  // "—" and the slider stays disabled, so this isn't read as a real setting.
  slider.value = "50";
  slider.disabled = true;
  initSlider(slider);

  const valueLabel = textEl("span", "set-slider-read");
  valueLabel.textContent = "—"; // nothing until /robot/info reports the live volume
  const sliderContainer = textEl("div", "set-slider-wrap");
  sliderContainer.append(valueLabel, slider);

  const status = textEl("span", "set-card-volume-status set-status muted");
  controlGroup.append(textEl("span", "set-validation-slot"), sliderContainer, status);
  controlContainer.appendChild(controlGroup);

  row.append(buildRowText(labelText, descriptionText), controlContainer);
  enableRowClick(row);
  section.appendChild(row);

  // Last percent known to be applied on the robot; the revert target on failure.
  let robotPercent = DEFAULT_VOLUME;
  let hasValue = false; // false until /robot/info reports the live volume
  let dragging = false;
  let saving = false;

  const setLiveStatus = (/** @type {string} */ msg, /** @type {string} */ cls) => {
    status.textContent = msg;
    status.className = "set-card-volume-status set-status " + cls;
  };

  const renderValue = (/** @type {number} */ percent) => {
    slider.value = String(percent);
    syncSliderFill(slider);
    valueLabel.textContent = percent + "%";
  };

  // Disabled until connected AND the live volume has loaded (so the page never
  // shows a guessed default), and while a save is in flight so a mid-save release
  // can't be silently dropped (nor re-enabled by a state change). Clearing
  // `dragging` on disable matters too: a disconnect mid-drag would otherwise
  // leave it stuck true, so the subscription below would ignore every /robot/info
  // after reconnect and the slider would freeze, diverging from the real volume.
  const refreshEnabled = () => {
    const shouldDisable = ros.state !== "connected" || saving || !hasValue;
    if (shouldDisable) dragging = false;
    slider.disabled = shouldDisable;
  };

  cleanups.push(
    ros.onStateChange((state) => {
      const connected = state === "connected";
      refreshEnabled();
      if (!connected) setLiveStatus("Connect to the robot to set volume.", "muted");
      else if (status.classList.contains("muted")) setLiveStatus("", "muted");
    }),
  );

  const unsubInfo = ros.subscribe(ROBOT_INFO_TOPIC, (/** @type {StringMsg} */ payload) => {
    /** @type {RobotInfo} */
    let infoData;
    try {
      infoData = JSON.parse(payload.data);
    } catch {
      return;
    }
    if (typeof infoData.volume_percent !== "number") return;
    robotPercent = clampVolume(infoData.volume_percent);
    const firstValue = !hasValue;
    hasValue = true;
    // Don't clobber a value the operator is actively dragging or saving.
    if (!dragging && !saving) renderValue(robotPercent);
    if (firstValue) refreshEnabled(); // enable now that the live value has loaded
  }, undefined, "std_msgs/msg/String");
  cleanups.push(unsubInfo);

  slider.addEventListener("input", () => {
    dragging = true;
    valueLabel.textContent = slider.value + "%";
  });

  slider.addEventListener("change", async () => {
    dragging = false;
    const next = clampVolume(Number(slider.value));
    renderValue(next);
    if (next === robotPercent || saving) return;
    const previous = robotPercent;
    saving = true;
    refreshEnabled();
    setLiveStatus("Saving…", "muted");
    try {
      /** @type {{ success: boolean, message?: string }} */
      const res = await ros.callService(SET_VOLUME_SERVICE, { volume_percent: next });
      if (!res.success) throw new Error(res.message || "Failed to set volume.");
      robotPercent = next;
      setLiveStatus("Volume set.", "ok");
    } catch {
      // Re-seed robotPercent too: a /robot/info update may have moved it during
      // the save, and on failure the robot's volume is still `previous`.
      robotPercent = previous;
      renderValue(previous);
      setLiveStatus("Couldn't set volume. Try again.", "err");
    } finally {
      saving = false;
      refreshEnabled();
    }
  });

  return { section, row, label: labelText, description: descriptionText };
}

const GROUP_CHEV =
  '<svg class="set-group-chev" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="9,6 15,12 9,18"/></svg>';

function setGroupOpen(/** @type {GroupUI} */ ui, /** @type {boolean} */ open) {
  ui.section.classList.toggle("open", open);
  ui.section.querySelector(".set-group-h")?.setAttribute("aria-expanded", String(open));
}

function buildSettingsPage() {
  styleEl = document.createElement("style");
  styleEl.textContent = SETTINGS_STYLE;
  document.head.appendChild(styleEl);

  const page = textEl("div", "settings-page");

  const body = textEl("div", "settings-body");

  const scroll = textEl("div", "settings-scroll");

  const wrap = textEl("div", "settings-wrap");

  const search = inputEl("search", "set-search");
  search.placeholder = "Search settings";
  search.maxLength = 40;
  search.setAttribute("aria-label", "Search settings");
  searchInput = search;

  const accordion = textEl("div", "set-accordion");

  const searchResults = textEl("div", "set-card-search");
  searchResults.hidden = true;

  wrap.append(
    textEl("h1", "page-title", "Settings"),
    textEl(
      "p",
      "settings-note",
      "Changes save to config/settings.yaml. Saving applies what it can immediately and says so; the rest take effect on the next restart.",
    ),
    search,
    accordion,
    searchResults,
  );

  for (const settingsPage of SETTINGS_PAGES) {
    const section = textEl("section", "set-group");

    const header = buttonEl("set-group-h", "");
    header.setAttribute("aria-expanded", "false");

    const icon = textEl("span", "set-group-icon");
    icon.style.setProperty(
      "--icon-url",
      `url("/js/settings/icons/${settingsPage.icon}")`,
    );

    const text = textEl("span", "set-group-text");
    const label = textEl("span", "set-group-label", settingsPage.title);
    const summary = textEl("span", "set-group-summary", settingsPage.summary);
    text.append(label, summary);

    const dot = textEl("span", "set-group-dot");
    dot.title = "Unsaved changes in this section";

    header.append(icon, text, dot);
    header.insertAdjacentHTML("beforeend", GROUP_CHEV);

    const groupBody = textEl("div", "set-group-body");
    const groupClip = textEl("div", "set-group-body-inner");
    const groupInner = textEl("div", "set-group-content");
    groupClip.appendChild(groupInner);
    groupBody.appendChild(groupClip);

    if (settingsPage.note) {
      groupInner.appendChild(textEl("p", "set-group-note", settingsPage.note));
    }

    section.append(header, groupBody);
    accordion.appendChild(section);

    const pageSearchSource = {
      label: "Matched in page description",
      text: settingsPage.summary,
    };
    const volumeControl = settingsPage.hasSpeakerVolume ? buildVolumeSection() : null;
    if (volumeControl) {
      const speaker = textEl("section", "set-page-section");
      speaker.append(textEl("h2", "set-section-title", "Speaker"), volumeControl.section);
      groupInner.appendChild(speaker);
    }

    /** @type {GroupUI} */
    const ui = { section, dot, entries: [] };
    header.addEventListener("click", () => setGroupOpen(ui, !section.classList.contains("open")));
    groups.push(ui);

    if (volumeControl) {
      addSearchTarget({
        label: volumeControl.label,
        description: volumeControl.description,
        row: volumeControl.row,
        group: ui,
        breadcrumb: `${settingsPage.title} · Speaker`,
        extraSearchSources: [pageSearchSource],
      });
    }

    for (const pageSection of settingsPage.sections) {
      const sectionStart = entries.length;
      groupInner.appendChild(buildPageSection(pageSection));
      const sectionEntries = entries.slice(sectionStart);
      ui.entries.push(...sectionEntries);
      const extraSearchSources = [
        { label: "Matched in section description", text: pageSection.note || "" },
        pageSearchSource,
      ].filter((source) => source.text);
      for (const entry of sectionEntries) {
        const breadcrumb = entry.knob.subsection
          ? `${settingsPage.title} · ${pageSection.title} · ${entry.knob.subsection}`
          : `${settingsPage.title} · ${pageSection.title}`;
        addSearchTarget({
          label: entry.knob.label,
          description: entry.knob.doc,
          row: entry.row,
          group: ui,
          breadcrumb,
          extraSearchSources,
        });
      }
    }
  }

  search.addEventListener("input", () => renderSearchResults(search.value, accordion, searchResults));

  scroll.appendChild(wrap);
  body.appendChild(scroll);
  page.appendChild(body);

  const bar = textEl("div", "set-bar");
  saveBtn = buttonEl("set-save", "Save", onSave);
  saveBtn.title = "Write these overrides to the robot's settings file";
  saveBtn.disabled = true;
  dirtyEl = textEl("span", "set-dirty");
  resetAllBtn = buttonEl("set-reset-all", "Reset all to defaults", resetAll);
  resetAllBtn.title = "Clear every override in the form — click Save to commit";
  resetAllBtn.disabled = true;
  restartBtn = buttonEl("set-restart", "Restart robot", onRestart);
  restartBtn.title = "Restart the robot to apply saved settings (same as innate restart)";
  shutdownBtn = buttonEl("set-restart set-shutdown", "Shut down", onShutdown);
  shutdownBtn.title = "Power off the Jetson (unplug and replug the battery to bring it back up)";
  statusEl = document.createElement("span");
  bar.append(saveBtn, dirtyEl, statusEl, resetAllBtn, restartBtn, shutdownBtn);
  page.appendChild(bar);

  stage.appendChild(page);
  setStatus("Loading current values…");
}

/** @param {import("./catalog.js").Knob[]} knobs */
function splitSubsectionGroups(knobs) {
  /** @type {{ title: string | null, knobs: import("./catalog.js").Knob[] }[]} */
  const groups = [];
  for (const knob of knobs) {
    const subsection = knob.subsection || null;
    const last = groups[groups.length - 1];
    if (last && last.title === subsection) last.knobs.push(knob);
    else groups.push({ title: subsection, knobs: [knob] });
  }
  return groups;
}

/** @param {import("./catalog.js").PageSection} pageSection */
function buildPageSection(pageSection) {
  const section = textEl("section", "set-page-section");
  section.appendChild(textEl("h2", "set-section-title", pageSection.title));
  if (pageSection.note) {
    section.appendChild(textEl("p", "set-section-note", pageSection.note));
  }

  const card = textEl("div", "set-card");
  const groups = splitSubsectionGroups(pageSection.knobs);
  const showSubsectionTitles = groups.filter((group) => group.title).length > 1;
  for (const group of groups) {
    /** @type {HTMLElement} */
    let host = card;
    if (showSubsectionTitles) {
      const block = textEl("div", "set-subblock");
      if (group.title) block.appendChild(textEl("div", "set-subh", group.title));
      card.appendChild(block);
      host = block;
    }
    for (const knob of group.knobs) host.appendChild(buildRow(knob));
  }
  section.appendChild(card);
  return section;
}

/**
 * Highlight via text nodes, never innerHTML (query stays safe).
 * @param {HTMLElement} el
 * @param {string} text
 * @param {string[]} terms  Lowercase search tokens already filtered for emptiness.
 */
function setHighlightedText(el, text, terms) {
  el.replaceChildren();
  const pattern = terms
    .map((term) => term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
    .sort((a, b) => b.length - a.length)
    .join("|");
  const re = new RegExp(pattern, "gi");
  let last = 0;
  for (const match of text.matchAll(re)) {
    const start = match.index ?? 0;
    if (start > last) el.append(text.slice(last, start));
    const mark = textEl("mark", "set-search-mark", match[0]);
    el.append(mark);
    last = start + match[0].length;
  }
  if (last < text.length) el.append(text.slice(last));
}

function searchResultRank(
  /** @type {SearchTarget} */ target,
  /** @type {string} */ normalizedQuery,
  /** @type {string[]} */ terms,
) {
  const labelText = target.label.toLowerCase();
  const descriptionText = target.description.toLowerCase();
  if (labelText === normalizedQuery) return 0;
  if (terms.every((term) => labelText.includes(term))) return 1;
  if (terms.every((term) => descriptionText.includes(term))) return 2;
  return 3;
}

function renderSearchResults(
  /** @type {string} */ query,
  /** @type {HTMLElement} */ accordion,
  /** @type {HTMLElement} */ resultsContainer,
) {
  const normalizedQuery = query.trim().toLowerCase();
  const terms = normalizedQuery.split(/\s+/).filter(Boolean);
  const hasSearchTerms = terms.length > 0;
  accordion.hidden = hasSearchTerms;
  resultsContainer.hidden = !hasSearchTerms;
  resultsContainer.replaceChildren();
  if (!hasSearchTerms) return;

  const matchingTargets = searchTargets
    .filter((target) => terms.every((term) => target.searchText.includes(term)))
    .sort(
      (a, b) =>
        searchResultRank(a, normalizedQuery, terms) -
        searchResultRank(b, normalizedQuery, terms),
    )
    .slice(0, MAX_SEARCH_RESULTS);

  if (!matchingTargets.length) {
    const emptyMessage = textEl("p", "set-search-empty", "No matching settings");
    resultsContainer.appendChild(emptyMessage);
    return;
  }

  for (const target of matchingTargets) {
    const resultButton = document.createElement("button");
    resultButton.type = "button";
    resultButton.className = "set-search-result";
    const label = textEl("span", "set-search-label");
    setHighlightedText(label, target.label, terms);
    const description = textEl("span", "set-search-doc");
    setHighlightedText(description, target.description, terms);
    const breadcrumb = textEl("span", "set-search-context");
    setHighlightedText(breadcrumb, target.breadcrumb, terms);
    resultButton.append(label, description, breadcrumb);

    let unshownTerms = terms.filter((term) => !target.visibleSearchText.includes(term));
    for (const source of target.extraSearchSources) {
      const normalizedSourceText = source.text.toLowerCase();
      const showsUnshownTerm = unshownTerms.some((term) => normalizedSourceText.includes(term));
      if (!showsUnshownTerm) continue;
      const matchedSource = textEl("span", "set-search-source");
      matchedSource.appendChild(textEl("span", "set-search-source-kind", source.label));
      const matchedSourceText = textEl("span", "set-search-source-text");
      setHighlightedText(matchedSourceText, source.text, terms);
      matchedSource.appendChild(matchedSourceText);
      resultButton.appendChild(matchedSource);
      unshownTerms = unshownTerms.filter((term) => !normalizedSourceText.includes(term));
    }
    resultButton.addEventListener("click", () => {
      // Clearing the query puts the accordion back on screen — scrolling to a row
      // inside a hidden container would land nowhere.
      if (searchInput) searchInput.value = "";
      renderSearchResults("", accordion, resultsContainer);
      const wasOpen = target.group.section.classList.contains("open");
      setGroupOpen(target.group, true);
      // Scrolling before the group finishes expanding aims at a row still collapsed to nothing.
      setTimeout(() => {
        target.row.scrollIntoView({ block: "center", behavior: "smooth" });
        const targetControl = target.row.querySelector("input, select, textarea, button");
        if (targetControl instanceof HTMLElement) focusControl(targetControl, { preventScroll: true });
        target.row.classList.add("search-hit");
        target.row.addEventListener(
          "animationend",
          () => target.row.classList.remove("search-hit"),
          { once: true },
        );
      }, wasOpen ? 0 : GROUP_EXPAND_MS);
    });
    resultsContainer.appendChild(resultButton);
  }
}

function buildRow(/** @type {import("./catalog.js").Knob} */ knob) {
  const row = textEl("div", "set-row");
  if (knob.type === "bool") row.classList.add("set-row-toggle");

  const controlContainer = textEl("div", "set-ctl");

  /** @type {Entry} */
  const entry = {
    knob,
    overridden: false,
    value: knob.default,
    savedOverridden: false,
    savedValue: knob.default,
    render: () => {},
    row,
  };

  buildKnobControl(controlContainer, entry);
  const link = knob.docHref
    ? { href: knob.docHref, text: knob.docLinkText || "Learn more" }
    : null;

  row.append(buildRowText(knob.label, knob.doc, link), controlContainer);
  enableRowClick(row);
  entries.push(entry);
  return row;
}

/** Clicking non-interactive row space activates its control. */
function enableRowClick(/** @type {HTMLElement} */ row) {
  row.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof Element) || target.closest("a, button, input, select, textarea, label")) return;
    activateRowControl(row);
  });
}

function activateRowControl(/** @type {HTMLElement} */ row) {
  const control = row.querySelector(
    "input.set-num, input.set-text, input[type=checkbox], input[type=range], select.set-text",
  );
  if (!(control instanceof HTMLElement)) return;
  if (control instanceof HTMLInputElement && control.type === "checkbox") {
    control.click();
    return;
  }
  focusControl(control);
  if (control instanceof HTMLSelectElement) {
    control.click();
    return;
  }
  if (control instanceof HTMLInputElement && control.type !== "range" && typeof control.select === "function") {
    control.select();
  }
}

function buildKnobControl(/** @type {HTMLElement} */ controlContainer, /** @type {Entry} */ entry) {
  const knob = entry.knob;
  const isSlider =
    (knob.type === "int" || knob.type === "float") && knob.slider === true && knob.max !== undefined;

  const controlGroup = textEl("div", "set-ctl-main");
  if (knob.type === "bool") controlGroup.classList.add("is-toggle");
  else if (knob.options || knob.type === "string" || isSlider) controlGroup.classList.add("is-wide");
  if (isSlider) controlGroup.classList.add("is-slider");

  const controlRow = textEl("div", "set-ctl-row");
  if (knob.type !== "bool") {
    const validation = textEl("span", "set-validation-slot");
    if ((knob.type === "int" || knob.type === "float") && !isSlider) {
      entry.validationEl = validation;
    }
    controlGroup.appendChild(validation);
  }

  if (knob.type === "bool") {
    const input = inputEl("checkbox");
    controlRow.appendChild(input);
    entry.render = () => {
      input.checked = Boolean(entry.value);
    };
    input.addEventListener("change", () => setKnobValue(entry, input.checked));
  } else if (isSlider) {
    const slider = inputEl("range", "set-slider");
    slider.min = String(knob.min ?? 0);
    slider.max = String(knob.max);
    slider.step = String(knob.step ?? 1);
    initSlider(slider);

    const valueLabel = textEl("span", "set-slider-read");
    const suffix = knob.unit ? (knob.unit === "%" ? "%" : " " + knob.unit) : "";
    const renderValue = (/** @type {number} */ value) => (valueLabel.textContent = value + suffix);
    const sliderContainer = textEl("div", "set-slider-wrap");
    sliderContainer.append(valueLabel, slider);
    controlRow.appendChild(sliderContainer);

    entry.render = () => {
      slider.value = String(entry.value);
      syncSliderFill(slider);
      renderValue(entry.value);
    };
    slider.addEventListener("input", () => {
      const value = Number(slider.value);
      renderValue(value);
      setKnobValue(entry, value);
    });
  } else if (knob.options) {
    const options = knob.options; // capture: the closures below can't re-narrow it
    const CUSTOM_OPTION_VALUE = "__custom__";
    const select = document.createElement("select");
    select.className = "set-text";
    const fillSelect = () => {
      select.replaceChildren();
      for (const option of options) {
        select.add(new Option(option.label, option.value));
      }
      // A permanent "Custom…" choice that reveals a free-text field for any off-list value
      // (e.g. a voice id pasted from Cartesia's library, or one set over SSH).
      select.add(new Option("Custom…", CUSTOM_OPTION_VALUE));
    };
    fillSelect();
    controlRow.appendChild(select);

    const customInput = inputEl("text", "set-text");
    customInput.placeholder = knob.customPlaceholder || "Paste a voice ID";
    customInput.style.display = "none";

    const isStockOption = () => options.some((option) => option.value === entry.value);
    // Options that arrived from the robot rather than the catalog. Mutating the captured
    // array in place is what keeps isStockOption honest, and the re-render is what moves a
    // saved value out of the Custom field once the list naming it lands.
    entry.setOptions = (list) => {
      options.length = 0;
      options.push(...list);
      fillSelect();
      entry.render();
    };
    entry.render = () => {
      const usesStockOption = isStockOption();
      select.value = usesStockOption ? String(entry.value) : CUSTOM_OPTION_VALUE;
      customInput.value = usesStockOption ? "" : String(entry.value);
      customInput.style.display = usesStockOption ? "none" : "";
    };
    select.addEventListener("change", () => {
      const usesCustomValue = select.value === CUSTOM_OPTION_VALUE;
      customInput.style.display = usesCustomValue ? "" : "none";
      if (usesCustomValue) {
        // Just reveal the field — don't commit an empty id. The input handler
        // commits once the user actually types one.
        customInput.focus();
        return;
      }
      setKnobValue(entry, select.value);
    });
    customInput.addEventListener("input", () => {
      // An empty id is never valid (it breaks TTS), so it isn't a real override.
      if (customInput.value === "") {
        entry.value = "";
        entry.overridden = false;
        recompute();
      } else {
        setKnobValue(entry, customInput.value);
      }
    });
    controlGroup.append(controlRow, customInput);
  } else if (knob.type === "string") {
    const input = inputEl("text", "set-text");
    controlRow.appendChild(input);
    entry.render = () => {
      input.value = String(entry.value);
    };
    input.addEventListener("input", () => setKnobValue(entry, input.value));
  } else {
    const numberInputContainer = textEl("div", "set-num-wrap");
    const input = inputEl("text", "set-num");
    // type=text (not number) so the decimal separator always renders as a dot,
    // regardless of the browser/OS locale; inputmode keeps the mobile numpad.
    input.inputMode = "decimal";
    if (knob.min !== undefined && knob.max !== undefined) {
      input.title = `Built-in default ${knob.default}` + (knob.unit ? ` ${knob.unit}` : "") + ` · range ${knob.min}–${knob.max}`;
    }
    numberInputContainer.appendChild(input);
    if (knob.unit) {
      numberInputContainer.appendChild(textEl("span", "set-unit", knob.unit));
    }
    controlRow.appendChild(numberInputContainer);
    numberInputContainer.addEventListener("click", (event) => {
      if (event.target !== input) input.focus();
    });
    entry.render = () => {
      input.value = String(entry.value);
    };
    input.addEventListener("input", () => {
      // Tolerate a comma decimal separator from locale-habit typing.
      setKnobValue(entry, Number(input.value.replace(",", ".")));
    });
  }

  if (!knob.options) controlGroup.appendChild(controlRow);
  if (knob.type !== "bool") controlGroup.appendChild(buildRestoreDefaultButton(entry));

  controlContainer.appendChild(controlGroup);
  entry.render(); // initialise the control from entry.value (the default at build time)
}

async function loadSettings() {
  let data;
  try {
    const res = await fetch("/settings.json", { cache: "no-store" });
    data = await res.json();
  } catch {
    setStatus("Couldn't read current settings — showing defaults.", "err");
    return;
  }
  const overrides = (data && data.overrides) || {};
  for (const e of entries) {
    const v = getNestedValue(overrides, e.knob.path);
    if (v === undefined) continue;
    const val = coerceToKnobType(e.knob, v);
    e.overridden = e.savedOverridden = true;
    e.value = e.savedValue = val;
    e.render();
  }
  recompute();
  setStatus("");
}

/** "spoke_card" -> "Spoke Card". The robot ships values, not display names. */
function labelFor(/** @type {string} */ value) {
  return value.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/**
 * Fill the knobs whose choices the robot owns rather than the catalog, by reading the
 * read-only parameter each one names. A failure is survivable: the picker keeps its
 * "Custom…" entry, so the value is still typeable — better than a stale list baked in
 * here. Deferred until rosbridge connects, because on a cold load the socket is usually
 * still opening when the page builds.
 */
function loadRobotOptions() {
  const wanted = entries.filter((e) => e.knob.optionsFrom);
  if (!wanted.length) return;

  const read = async () => {
    for (const e of wanted) {
      const { node, param } = /** @type {{node: string, param: string}} */ (e.knob.optionsFrom);
      try {
        const res = await ros.callService(`${node}/get_parameters`, { names: [param] });
        const values = res?.values?.[0]?.string_array_value || [];
        if (values.length) e.setOptions?.(values.map((value) => ({ value, label: labelFor(value) })));
      } catch (err) {
        console.warn(`Couldn't read ${param} from ${node} — leaving the picker free-text:`, err);
      }
    }
  };

  if (ros.state === "connected") {
    read();
    return;
  }
  const unlisten = ros.onStateChange((/** @type {string} */ state) => {
    if (state !== "connected") return;
    unlisten();
    read();
  });
  cleanups.push(unlisten);
}

async function savePost(/** @type {any} */ payload) {
  const res = await fetch("/settings.json", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    cache: "no-store",
    body: JSON.stringify(payload),
  });
  // The proxy answers {ok, message} for both success and a rejected change (400
  // for a malformed body), so parse and let the caller read res.ok.
  return res.json();
}

/** rcl_interfaces/msg/ParameterType. Only the types the catalog can mark `live`. */
/** @type {Record<string, number>} */
const PARAM_TYPE = { bool: 1, int: 2, float: 3, string: 4 };

/**
 * Push saved knobs to their running node so they take effect without a restart.
 *
 * Only knobs the catalog marks `live` are sent — a node that copied the parameter into a
 * field at construction would accept the write and go on using the old value, and claiming
 * "applied" there would be worse than telling the operator to restart.
 *
 * Cleared knobs are pushed too, at their catalog default: reverting an override has to
 * reach the robot as well, or "reset" would only take effect on the next restart.
 *
 * @param {readonly Readonly<{
 *   overridden: boolean,
 *   value: any,
 *   previousSavedOverridden: boolean,
 *   previousSavedValue: any
 * }>[] } snapshot
 * @returns {Promise<{applied: number, failed: number, restart: number}>}
 */
async function applyLive(snapshot) {
  /** @type {Map<string, {name: string, value: any, type: string}[]>} */
  const byNode = new Map();
  let restart = 0;

  entries.forEach((e, i) => {
    const knob = e.knob;
    const changed =
      snapshot[i].overridden !== snapshot[i].previousSavedOverridden ||
      (snapshot[i].overridden
        && snapshot[i].value !== snapshot[i].previousSavedValue);
    if (!changed) return;
    if (!knob.live) {
      restart++;
      return;
    }
    const type = PARAM_TYPE[knob.type];
    if (!type) {
      restart++;
      return;
    }
    // path is [node, "ros__parameters", ...groups, key]; the ROS name is the dotted tail.
    const name = knob.path.slice(2).join(".");
    const value = snapshot[i].overridden ? snapshot[i].value : knob.default;
    const list = byNode.get(knob.live) || [];
    list.push({ name, value, type: knob.type });
    byNode.set(knob.live, list);
  });

  let applied = 0;
  let failed = 0;
  for (const [node, params] of byNode) {
    try {
      const res = await ros.callService(`${node}/set_parameters`, {
        parameters: params.map((p) => ({
          name: p.name,
          value: {
            type: PARAM_TYPE[p.type],
            bool_value: p.type === "bool" ? Boolean(p.value) : false,
            integer_value: p.type === "int" ? Number(p.value) : 0,
            double_value: p.type === "float" ? Number(p.value) : 0,
            string_value: p.type === "string" ? String(p.value) : "",
          },
        })),
      });
      // The service resolves even when a node rejects a value, so read each result.
      const results = res?.results || [];
      results.forEach((/** @type {{successful?: boolean}} */ r) =>
        (r?.successful === false ? failed++ : applied++));
      if (!results.length) failed += params.length;
    } catch (err) {
      console.warn(`Live-apply to ${node} failed:`, err);
      failed += params.length;
    }
  }
  return { applied, failed, restart };
}

async function onSave() {
  const sets = [];
  const clears = [];
  // Snapshot exactly what we send, keyed by entry index. The success handler
  // stamps saved-state from THIS snapshot, not the live entries — otherwise a
  // field edited during the WS round-trip would be mis-marked as saved while
  // the file holds the older value.
  const snapshot = Object.freeze(entries.map((e) => Object.freeze({
    overridden: e.overridden,
    value: e.value,
    previousSavedOverridden: e.savedOverridden,
    previousSavedValue: e.savedValue,
  })));
  for (const e of entries) {
    if (e.overridden) sets.push({ path: e.knob.path, value: e.value, type: e.knob.type });
    else clears.push(e.knob.path);
  }
  // The TTS voice used to be two per-node params (brain_client_node /
  // input_manager_node cartesia_voice_id) before it became the global `/**` knob.
  // A node-specific param beats a `/**` wildcard in ROS, so any leftover per-node
  // entries silently shadow the picker. The catalog no longer has those paths, so
  // its clears can't reach them — always clear them here so saving heals the orphans.
  for (const node of ["brain_client_node", "input_manager_node"]) {
    clears.push([node, "ros__parameters", "cartesia_voice_id"]);
  }
  saveBtn.disabled = true;
  setStatus("Saving…");
  try {
    const res = await savePost({ sets, clears });
    if (res && res.ok) {
      entries.forEach((e, i) => {
        e.savedOverridden = snapshot[i].overridden;
        e.savedValue = snapshot[i].value;
      });
      recompute();
      const { applied, failed, restart } = await applyLive(snapshot);
      const parts = [];
      if (applied) parts.push(`${applied} applied now`);
      if (restart) parts.push(`${restart} need${restart === 1 ? "s" : ""} a restart`);
      if (failed) parts.push(`${failed} could not be applied live`);
      setStatus(parts.length ? `Saved — ${parts.join(", ")}.` : "Saved.", failed ? "err" : "ok");
    } else {
      setStatus("Save failed: " + ((res && res.message) || "unknown error"), "err");
      recompute();
    }
  } catch (err) {
    setStatus("Save failed: " + (err instanceof Error ? err.message : String(err)), "err");
    recompute();
  }
}

async function onRestart() {
  if (!window.confirm("Restart the robot now? Any running task will stop, and the robot will come back in ~30s with the latest saved settings.")) {
    return;
  }
  restartBtn.disabled = true;
  setStatus("Restarting the robot…");
  try {
    const res = await fetch("/restart", { headers: { "X-Requested-By": "innate-webapp" } });
    if (res.ok) {
      // The proxy is torn down by the restart, so leave the button disabled —
      // the page reconnects on its own once the robot is back.
      setStatus("Restarting — the robot will be back in ~30s.", "ok");
    } else {
      setStatus("Restart failed: " + (await res.text().catch(() => "") || res.status), "err");
      restartBtn.disabled = false;
    }
  } catch (err) {
    setStatus("Couldn't reach the robot to restart: " + (err instanceof Error ? err.message : String(err)), "err");
    restartBtn.disabled = false;
  }
}

async function onShutdown() {
  if (!window.confirm("Shut down the robot now? The Jetson will power off — the only way back up is to unplug the battery and plug it in again.")) {
    return;
  }
  shutdownBtn.disabled = true;
  setStatus("Shutting down the robot…");
  const wasConnected = ros.state === "connected";
  try {
    const res = await ros.callService(SHUTDOWN_SERVICE, { delay_seconds: 0 });
    if (!res.success) {
      setStatus("Shutdown failed: " + (res.message || "unknown error"), "err");
      shutdownBtn.disabled = false;
      return;
    }
  } catch (err) {
    // The power-off can kill rosbridge before the response frame escapes, so a
    // link that was up at call time and dropped during it reads as a success —
    // but the same drop could be an unrelated blip that ate the request. A
    // reconnect settles it: whether the request was lost or the robot was
    // powered back on, a live link means the robot is up and the buttons must
    // work again, so the watch never expires — elapsed time only picks the
    // message.
    if (!wasConnected || ros.state === "connected") {
      setStatus("Couldn't reach the robot to shut down: " + (err instanceof Error ? err.message : String(err)), "err");
      shutdownBtn.disabled = false;
      return;
    }
    const droppedAt = performance.now();
    const unlisten = ros.onStateChange((state) => {
      if (state !== "connected") return;
      unlisten();
      shutdownBtn.disabled = false;
      restartBtn.disabled = false;
      if (performance.now() - droppedAt < SHUTDOWN_QUICK_RECONNECT_MS) {
        setStatus("The connection came back — the robot is still up, so the shutdown request was lost. Try again.", "err");
      } else {
        setStatus("Reconnected — the robot is back online.", "ok");
      }
    });
    cleanups.push(unlisten);
  }
  // The box is going down, so both power buttons stay disabled until the
  // operator power-cycles the battery and reloads.
  restartBtn.disabled = true;
  setStatus("Shutting down — the robot will power off in a few seconds.", "ok");
}

/** @param {HTMLElement} stageEl */
export function mount(stageEl) {
  stage = stageEl;
  cleanups = [];
  entries.length = 0;
  groups.length = 0;
  searchTargets.length = 0;
  searchInput = null;
  buildSettingsPage();
  loadSettings();
  loadRobotOptions();
  return {
    destroy() {
      for (const fn of cleanups.splice(0)) fn();
      styleEl?.remove();
      styleEl = null;
      searchInput = null;
      stage.replaceChildren();
    },
  };
}
