// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Settings › People — the owner's controls over what the robot remembers about
// the people it meets (docs/rfc/people-memory.md §10): the roster with a
// thumbnail next to every name, rename, merge, forget, and the collection
// switch that stops recognition without deleting anything.
//
// Everything here goes through the people node's services, so the page shows
// the robot's state, never a local copy of it: every mutation is followed by a
// fresh GetPeople rather than a hopeful in-place edit. The node answers from
// memory, so a call that has not come back in PEOPLE_SERVICE_TIMEOUT_MS means
// nothing is listening — an old robot, or the node down — and the card says so
// instead of showing an empty roster, which would read as "nobody is known".
//
// The parsing, request bodies, and row copy are pure and node-testable
// (tests/people.test.js); the DOM below is the only part that needs a browser.

import { ageText } from "../map/memories.js";
import {
  FORGET_PERSON_SERVICE,
  GET_PEOPLE_SERVICE,
  MERGE_PEOPLE_SERVICE,
  PEOPLE_RENAME_SOURCE_APP,
  PEOPLE_SERVICE_TIMEOUT_MS,
  RENAME_PERSON_SERVICE,
  SET_PEOPLE_COLLECTION_SERVICE,
} from "../constants.js";
import { confirmDialog, dismissAllConfirms } from "../nav/confirm.js";

export const PEOPLE_CARD_LABEL = "Recognize and remember people";
export const PEOPLE_CARD_DOC =
  "Faces, names, and what people told the robot, kept on the robot. Turning this off stops recognition and enrolment; it does not delete anyone.";

/** Shown while nobody is enrolled — true to how enrolment actually happens. */
export const PEOPLE_EMPTY_TEXT =
  "Nobody yet. The robot remembers someone after a few clear looks at their face, and learns a name when they say it or when you set one here.";
/** Shown when the people node does not answer. */
export const PEOPLE_UNAVAILABLE_TEXT =
  "People memory isn't answering — the people node isn't running on this robot.";
/** Shown when the store is at capacity: enrolment stops, names stay (RFC §9). */
export const PEOPLE_FULL_TEXT =
  "The roster is full, so no new people are being enrolled. Forget someone to make room.";

/** @typedef {{ id: string, name: string | null, unnamed: boolean, lastSeen: number, encounters: number, thumbnail: string | null, description: string | null }} RosterPerson */
/** @typedef {{ stamp: number, collectionEnabled: boolean, capacityFull: boolean, people: RosterPerson[] }} Roster */
/** @typedef {{ id: string, name: string, label: string, unnamed: boolean, meta: string, initial: string, thumbnailSrc: string | null, description: string | null, mergeOptions: { value: string, label: string }[] }} PersonRow */

// ---- request bodies (exactly the .srv request fields) ----------------------

/** GetPeople: the snapshot plus the roster the card lists, thumbnails included. */
export function getPeopleRequest() {
  return { include_roster: true, include_thumbnails: true };
}

/**
 * RenamePerson. `who` is a person id here (the card never renames a live tag),
 * and `source` records the consent path stored with the profile.
 * @param {string} who @param {string} name
 */
export function renamePersonRequest(who, name) {
  return { who, name, source: PEOPLE_RENAME_SOURCE_APP };
}

/** MergePeople: fold `sourceId` into `targetId`, keeping the target's identity.
 * @param {string} sourceId @param {string} targetId */
export function mergePeopleRequest(sourceId, targetId) {
  return { source_id: sourceId, target_id: targetId };
}

/** ForgetPerson: delete everything about them and tombstone the id.
 * @param {string} who */
export function forgetPersonRequest(who) {
  return { who };
}

/** SetPeopleCollection: the "never collect" preference.
 * @param {boolean} enabled */
export function setCollectionRequest(enabled) {
  return { enabled };
}

// ---- parsing and row copy --------------------------------------------------

/**
 * Parse GetPeople's `json` (the snapshot, plus `roster` when asked for it).
 * Null when the payload carries no roster at all — a robot whose people node
 * predates the roster field is "unavailable" here, not "empty".
 * @param {string} json
 * @returns {Roster | null}
 */
export function parseRoster(json) {
  /** @type {any} */
  let data;
  try {
    data = JSON.parse(json ?? "");
  } catch {
    return null;
  }
  if (!Array.isArray(data?.roster)) return null;
  /** @type {RosterPerson[]} */
  const people = [];
  for (const entry of data.roster) {
    const id = typeof entry?.person_id === "string" ? entry.person_id : entry?.id;
    if (typeof id !== "string" || !id) continue;
    const name = typeof entry.name === "string" && entry.name.trim() ? entry.name.trim() : null;
    people.push({
      id,
      name,
      unnamed: typeof entry.unnamed === "boolean" ? entry.unnamed : name === null,
      lastSeen: lastSeenStamp(entry.last_seen),
      encounters: typeof entry.encounters === "number" ? entry.encounters : 0,
      thumbnail: typeof entry.thumbnail === "string" && entry.thumbnail ? entry.thumbnail : null,
      description: typeof entry.description === "string" && entry.description ? entry.description : null,
    });
  }
  return {
    // The robot's own clock at the moment it answered: the only reference that
    // makes "5 min ago" right on a robot whose clock disagrees with the browser.
    stamp: typeof data.stamp === "number" ? data.stamp : 0,
    collectionEnabled: data.collection_enabled !== false,
    capacityFull: data.capacity_full === true,
    people,
  };
}

/** `last_seen` is the store's {stamp, map, x, y}; a bare epoch also reads.
 * @param {any} value @returns {number} */
function lastSeenStamp(value) {
  if (typeof value === "number") return value;
  if (typeof value?.stamp === "number") return value.stamp;
  return 0;
}

/** The 8 hex characters that distinguish one `person_7f92a1b3` from another.
 * @param {string} id */
export function shortId(id) {
  return id.startsWith("person_") ? id.slice("person_".length) : id;
}

/** @param {RosterPerson} person */
export function displayName(person) {
  return person.name ?? "unnamed";
}

/** Merge targets have to be told apart before they are picked, so an unnamed
 * record carries its id fragment.
 * @param {RosterPerson} person */
export function optionLabel(person) {
  return person.name ?? `unnamed ${shortId(person.id)}`;
}

/**
 * The one-line summary under a name: when they were last seen and how often
 * the robot has met them.
 * @param {RosterPerson} person @param {number} now epoch seconds
 */
export function personMeta(person, now) {
  const seen = person.lastSeen > 0 ? `Last seen ${ageText(person.lastSeen, now)}` : "Not seen yet";
  const count = person.encounters === 1 ? "1 encounter" : `${person.encounters} encounters`;
  return `${seen} · ${count}`;
}

/** A thumbnail as an <img> src: base64 JPEG off the service, or a data URL if
 * the robot already framed it as one. Null when there is no thumbnail.
 * @param {RosterPerson} person @returns {string | null} */
export function thumbnailSrc(person) {
  if (!person.thumbnail) return null;
  return person.thumbnail.startsWith("data:") ? person.thumbnail : `data:image/jpeg;base64,${person.thumbnail}`;
}

/**
 * The roster as the card renders it: display strings, thumbnail sources, and
 * each row's merge targets (everyone but themselves, most recent first).
 * @param {Roster} roster
 * @param {number} [now] epoch seconds; defaults to the robot's own stamp
 * @returns {PersonRow[]}
 */
export function rosterRows(roster, now = roster.stamp || Date.now() / 1000) {
  const ordered = [...roster.people].sort((a, b) => b.lastSeen - a.lastSeen);
  return ordered.map((person) => ({
    id: person.id,
    name: displayName(person),
    // Dialogs and screen readers need to name one record among several, which
    // "unnamed" alone does not do.
    label: optionLabel(person),
    unnamed: person.unnamed,
    meta: personMeta(person, now),
    initial: (person.name ?? "?").trim().charAt(0).toUpperCase() || "?",
    thumbnailSrc: thumbnailSrc(person),
    description: person.description,
    mergeOptions: ordered
      .filter((other) => other.id !== person.id)
      .map((other) => ({ value: other.id, label: optionLabel(other) })),
  }));
}

// ---- the card --------------------------------------------------------------

/** @param {string} tag @param {string} className @param {string} [text] */
function el(tag, className, text) {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/** @param {string} className @param {string} text @param {() => void} onClick */
function button(className, text, onClick) {
  const node = document.createElement("button");
  node.type = "button";
  node.className = className;
  node.textContent = text;
  node.addEventListener("click", onClick);
  return node;
}

/**
 * Build the People card. `ros` is injected rather than imported so this module
 * stays free of the shared socket's page-lifecycle listeners and the pure
 * helpers above can be imported headlessly.
 * @param {import("../rosClient.js").RosClient} ros
 * @returns {{ section: HTMLElement, row: HTMLElement, label: string, description: string, destroy: () => void }}
 */
export function createPeopleCard(ros) {
  const section = el("section", "set-card set-card-people");

  const row = el("div", "set-row set-row-toggle");
  const control = el("div", "set-ctl");
  const controlGroup = el("div", "set-ctl-main is-toggle");
  const toggle = document.createElement("input");
  toggle.type = "checkbox";
  toggle.disabled = true;
  toggle.title = `${SET_PEOPLE_COLLECTION_SERVICE} — stops recognition and enrolment; deletes nothing`;
  toggle.setAttribute("aria-label", PEOPLE_CARD_LABEL);
  controlGroup.appendChild(toggle);
  control.appendChild(controlGroup);
  const info = el("div", "set-info");
  info.append(el("span", "set-label", PEOPLE_CARD_LABEL), el("span", "set-doc", PEOPLE_CARD_DOC));
  row.append(info, control);
  // Every other row on this page flips its control when the copy is clicked
  // (main.js's enableRowClick, private to that module).
  row.addEventListener("click", (event) => {
    const target = event.target;
    if (target instanceof Element && target.closest("a, button, input, select, textarea, label")) return;
    if (!toggle.disabled) toggle.click();
  });

  const status = el("p", "set-people-status set-status muted", "Loading the roster…");
  const list = el("div", "set-people-list");
  const retry = button("set-people-btn", "Try again", () => void refresh());
  retry.hidden = true;
  const foot = el("div", "set-people-foot");
  foot.append(status, retry);
  section.append(row, list, foot);

  /** @type {Roster | null} */
  let roster = null;
  let busy = false;
  let destroyed = false;

  /** @param {string} message @param {"ok" | "err" | "muted"} kind */
  function setStatus(message, kind) {
    status.textContent = message;
    status.className = `set-people-status set-status ${kind}`;
    status.hidden = !message;
  }

  /** Everything the card can do is one service call away, so a call in flight
   * disables the lot rather than racing itself.
   * @param {boolean} next */
  function setBusy(next) {
    busy = next;
    section.classList.toggle("is-busy", next);
    for (const node of section.querySelectorAll("button, select")) {
      /** @type {HTMLButtonElement | HTMLSelectElement} */ (node).disabled = next;
    }
    toggle.disabled = next || !roster || ros.state !== "connected";
  }

  /**
   * Run one people-node call and reload the roster from the robot afterwards,
   * whatever happened: the store is the truth, and a failed merge or a partial
   * forget must not leave the card showing what the operator intended.
   * @param {string} service @param {object} args @param {string} failure
   */
  async function mutate(service, args, failure) {
    if (busy) return;
    setBusy(true);
    setStatus("Working…", "muted");
    try {
      /** @type {{ success?: boolean, message?: string }} */
      const res = await ros.callService(service, args, PEOPLE_SERVICE_TIMEOUT_MS);
      if (res?.success === false) throw new Error(res.message || failure);
    } catch (err) {
      if (destroyed) return;
      setBusy(false);
      setStatus(`${failure}: ${err instanceof Error ? err.message : String(err)}`, "err");
      await refresh(true);
      return;
    }
    if (destroyed) return;
    setBusy(false);
    await refresh();
  }

  /** @param {boolean} [keepStatus] leave a failure message in place */
  async function refresh(keepStatus = false) {
    if (ros.state !== "connected") {
      roster = null;
      render();
      setStatus("Connect to the robot to see who it knows.", "muted");
      retry.hidden = true;
      return;
    }
    if (!keepStatus) setStatus("Loading the roster…", "muted");
    try {
      /** @type {{ success?: boolean, message?: string, json?: string }} */
      const res = await ros.callService(GET_PEOPLE_SERVICE, getPeopleRequest(), PEOPLE_SERVICE_TIMEOUT_MS);
      if (destroyed) return;
      const parsed = res?.success === false ? null : parseRoster(res?.json ?? "");
      roster = parsed;
      render();
      if (!parsed) {
        setStatus(res?.message || PEOPLE_UNAVAILABLE_TEXT, "err");
        retry.hidden = false;
        return;
      }
      retry.hidden = true;
      if (!keepStatus) setStatus(parsed.capacityFull ? PEOPLE_FULL_TEXT : "", parsed.capacityFull ? "err" : "muted");
    } catch (err) {
      if (destroyed) return;
      roster = null;
      render();
      // A timeout is the expected shape of "no such service here", so it reads
      // as the unavailable state rather than as an error the operator caused.
      setStatus(PEOPLE_UNAVAILABLE_TEXT, "err");
      retry.hidden = false;
      console.warn("[settings] people roster unavailable:", err);
    }
  }

  function render() {
    list.replaceChildren();
    toggle.checked = roster ? roster.collectionEnabled : false;
    toggle.disabled = busy || !roster || ros.state !== "connected";
    if (!roster) return;
    if (!roster.people.length) {
      list.appendChild(el("p", "set-people-empty", PEOPLE_EMPTY_TEXT));
      return;
    }
    for (const person of rosterRows(roster)) list.appendChild(buildRow(person));
  }

  /** @param {PersonRow} person */
  function buildRow(person) {
    const personRow = el("div", "set-people-row");
    if (person.description) personRow.title = person.description;

    const thumb = el("span", "set-people-thumb");
    if (person.thumbnailSrc) {
      const img = document.createElement("img");
      img.src = person.thumbnailSrc;
      img.alt = "";
      thumb.appendChild(img);
    } else {
      thumb.classList.add("is-empty");
      thumb.textContent = person.initial;
    }

    const text = el("div", "set-people-text");
    const name = el("span", "set-people-name", person.name);
    if (person.unnamed) name.classList.add("is-unnamed");
    text.append(name, el("span", "set-people-meta", person.meta));

    const actions = el("div", "set-people-actions");
    const rename = button("set-people-btn", "Rename", () => startRename(person, text, name));
    rename.title = `${RENAME_PERSON_SERVICE} — the robot uses this name from now on`;
    actions.append(rename, buildMerge(person), buildForget(person));

    personRow.append(thumb, text, actions);
    return personRow;
  }

  /** @param {PersonRow} person */
  function buildMerge(person) {
    const merge = document.createElement("select");
    merge.className = "set-people-merge";
    merge.title = `${MERGE_PEOPLE_SERVICE} — fold this record into another one`;
    merge.setAttribute("aria-label", `Merge ${person.label} into another person`);
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "Merge into…";
    merge.appendChild(placeholder);
    for (const option of person.mergeOptions) {
      const node = document.createElement("option");
      node.value = option.value;
      node.textContent = option.label;
      merge.appendChild(node);
    }
    merge.disabled = busy || person.mergeOptions.length === 0;
    merge.addEventListener("change", async () => {
      const targetId = merge.value;
      merge.value = "";
      if (!targetId) return;
      const target = person.mergeOptions.find((option) => option.value === targetId);
      const ok = await confirmDialog({
        title: "Merge these two?",
        body: `Everything the robot knows about ${person.label} moves into ${target?.label ?? "the other record"}, which keeps its name. This cannot be undone.`,
        confirmLabel: "Merge",
        danger: true,
      });
      if (!ok) return;
      await mutate(MERGE_PEOPLE_SERVICE, mergePeopleRequest(person.id, targetId), "Couldn't merge");
    });
    return merge;
  }

  /** @param {PersonRow} person */
  function buildForget(person) {
    const forget = button("set-people-btn is-danger", "Forget", async () => {
      const ok = await confirmDialog({
        title: `Forget ${person.label}?`,
        body: "Their face, description, facts, and history are deleted from the robot. If the robot meets them again it starts from nothing.",
        confirmLabel: "Forget",
        danger: true,
      });
      if (!ok) return;
      await mutate(FORGET_PERSON_SERVICE, forgetPersonRequest(person.id), "Couldn't forget");
    });
    forget.title = `${FORGET_PERSON_SERVICE} — delete everything about this person`;
    return forget;
  }

  /** Inline editor: the name becomes a field in place, so the row keeps its
   * thumbnail in view while the operator types (a name is checked against a
   * face, not remembered from the row above).
   * @param {PersonRow} person @param {HTMLElement} text @param {HTMLElement} name */
  function startRename(person, text, name) {
    if (busy || text.querySelector(".set-people-name-input")) return;
    const input = document.createElement("input");
    input.type = "text";
    input.className = "set-people-name-input";
    input.value = person.unnamed ? "" : person.name;
    input.placeholder = "Name";
    input.maxLength = 60;
    input.setAttribute("aria-label", "Name");

    const editor = el("span", "set-people-edit");
    const cancel = () => {
      input.replaceWith(name);
      editor.remove();
    };
    const commit = async () => {
      const next = input.value.trim();
      cancel();
      if (!next || next === (person.unnamed ? "" : person.name)) return;
      await mutate(RENAME_PERSON_SERVICE, renamePersonRequest(person.id, next), "Couldn't rename");
    };
    editor.append(button("set-people-btn", "Save", () => void commit()), button("set-people-btn", "Cancel", cancel));
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") void commit();
      else if (event.key === "Escape") cancel();
    });
    name.replaceWith(input);
    text.appendChild(editor);
    input.focus();
    input.select();
  }

  toggle.addEventListener("change", () => {
    const enabled = toggle.checked;
    // Snap back until the robot confirms: the switch reports the store's state,
    // not the click.
    toggle.checked = roster ? roster.collectionEnabled : false;
    void mutate(SET_PEOPLE_COLLECTION_SERVICE, setCollectionRequest(enabled), "Couldn't change the setting");
  });

  const unsubState = ros.onStateChange(() => {
    // Fires immediately with the current state, which is what loads the card.
    void refresh();
  });

  return {
    section,
    row,
    label: PEOPLE_CARD_LABEL,
    description: PEOPLE_CARD_DOC,
    destroy() {
      destroyed = true;
      unsubState();
      dismissAllConfirms();
    },
  };
}

/** Card-scoped CSS, appended to the Settings page's injected stylesheet. */
export const PEOPLE_STYLE = `
.set-people-row { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 12px;
  align-items: center; padding: 12px 20px; border-bottom: 1px solid var(--hairline, #2a2f3a); }
.set-people-list > .set-people-row:last-child { border-bottom: none; }
.set-people-thumb { display: flex; align-items: center; justify-content: center;
  width: 40px; height: 40px; overflow: hidden; border-radius: 8px;
  background: rgba(255,255,255,.06); color: var(--muted, #8a90a0);
  font-size: 15px; font-weight: 600; }
.set-people-thumb img { width: 100%; height: 100%; object-fit: cover; }
.set-people-text { min-width: 0; display: flex; flex-direction: column; gap: 3px; }
.set-people-name { font-size: 13px; font-weight: 500; line-height: 1.35;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.set-people-name.is-unnamed { color: var(--muted, #8a90a0); font-style: italic; }
.set-people-meta { color: var(--muted, #8a90a0); font-size: 12px; line-height: 1.4; }
/* The page's field styling lives under .set-ctl, which these two are not in. */
.set-people-name-input, .set-people-merge { box-sizing: border-box; border-radius: 6px;
  border: 1px solid var(--hairline, #2a2f3a); background-color: rgba(255,255,255,.03);
  color: inherit; font: inherit; }
.set-people-name-input:focus, .set-people-merge:focus { outline: none;
  border-color: rgba(117,105,253,.55); background-color: rgba(0,0,0,.2); }
.set-people-name-input { width: min(220px, 100%); padding: 5px 10px; font-size: 13px; }
.set-people-edit { display: flex; gap: 10px; }
.set-people-actions { display: flex; align-items: center; gap: 8px; }
.set-people-btn { padding: 5px 10px; border-radius: 6px; border: 1px solid var(--hairline, #2a2f3a);
  background: none; color: var(--text, #e7e7ea); font: inherit; font-size: 12px; cursor: pointer;
  transition: border-color .15s ease, color .15s ease; }
.set-people-btn:not(:disabled):hover { border-color: var(--primary, #7569FD); color: var(--primary, #7569FD); }
.set-people-btn.is-danger:not(:disabled):hover { border-color: #e95656; color: #e95656; }
.set-people-btn:disabled { opacity: .4; cursor: default; }
.set-people-merge { max-width: 150px; padding: 5px 30px 5px 10px; font-size: 12px; cursor: pointer; }
.set-people-merge:disabled { opacity: .4; cursor: default; }
.set-people-empty { margin: 0; padding: 16px 20px; color: var(--muted, #8a90a0);
  font-size: 12px; line-height: 1.5; }
.set-people-foot { display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 12px 20px; }
.set-people-foot:not(:has(> *:not([hidden]))) { display: none; }
.set-people-status { margin: 0; font-size: 12px; line-height: 1.45; }
.set-card-people.is-busy { opacity: .7; }

@media (max-width: 560px) {
  .set-people-row { grid-template-columns: auto minmax(0, 1fr); padding: 12px 14px; }
  .set-people-actions { grid-column: 1 / -1; justify-content: flex-start; flex-wrap: wrap; }
  .set-people-empty, .set-people-foot { padding: 14px; }
}
`;
