// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Shared by the Map page and Teleop; note pins never dispatch navigation.
/** @typedef {{map: string, fingerprint: string, frame?: string}} MapRef */
/** @typedef {{id: string, revision: number, title: string, text: string, x: number, y: number, anchor: string, observed_at: number, certainty: string, has_evidence: boolean}} Note */
/** @typedef {{map_ref: MapRef | null, revision: number, notes: Note[]}} Snapshot */
/** @typedef {{canvas: HTMLCanvasElement, viewKey: () => string, matchesMap: (ref: MapRef | null) => boolean, project: (x: number, y: number) => {x: number, y: number} | null, unproject: (e: PointerEvent) => {x: number, y: number} | null}} Geometry */
const TOPIC = "/brain/map_notes";

/** @param {MapRef | null | undefined} a @param {MapRef | null | undefined} b */
function sameMap(a, b) {
  return !!a && !!b && a.map === b.map && a.fingerprint === b.fingerprint && a.frame === b.frame;
}

/** @template {keyof HTMLElementTagNameMap} K @param {K} tag @param {string} [text] @param {string} [className] */
function el(tag, text = "", className = "") {
  const node = document.createElement(tag);
  node.textContent = text;
  node.className = className;
  return node;
}
/** @param {string} text @param {() => void} callback */
function button(text, callback) {
  const b = el("button", text);
  b.type = "button";
  b.addEventListener("click", callback);
  return b;
}
/** @param {unknown} error */
function errorText(error) {
  return error instanceof Error ? error.message : String(error);
}

/** @param {HTMLElement} root @param {import("../rosClient.js").RosClient} ros @param {Geometry} geometry */
export function createNotesOverlay(root, ros, geometry) {
  const layer = el("div", "", "map-notes-layer");
  const toggle = button("Notes", () => {
    panel.hidden = !panel.hidden;
    adding = false;
    editing = false;
    if (!panel.hidden) showPanel();
  });
  toggle.className = "map-notes-toggle";
  const panel = el("section", "", "map-note-panel");
  panel.hidden = true;
  panel.setAttribute("aria-label", "Map notes");
  root.append(layer, toggle, panel);
  /** @type {Snapshot | null} */
  let snapshot = null;
  /** @type {string | null} */
  let selected = null;
  let visible = true,
    editing = false,
    adding = false,
    busy = false,
    disposed = false,
    epoch = 0;
  let reloadPending = false;
  let renderedKey = "";
  /** @type {ReturnType<typeof setTimeout> | undefined} */
  let reloadTimer;
  /** @type {Map<string, HTMLButtonElement>} */
  const pins = new Map();
  for (const element of [layer, toggle, panel]) {
    for (const event of ["pointerdown", "pointerup", "click", "wheel"]) {
      element.addEventListener(event, (e) => e.stopPropagation());
    }
  }
  function active() {
    return !!snapshot && geometry.matchesMap(snapshot.map_ref);
  }
  /** @param {string} operation @param {object} [args] */
  async function request(operation, args = {}) {
    if (!active() && operation !== "snapshot") throw new Error("Map changed; wait for its notes to reload");
    const reply = await ros.callService(TOPIC, {
      request: JSON.stringify({
        operation,
        arguments: args,
        map_ref: snapshot?.map_ref,
        request_id: crypto.randomUUID(),
      }),
    });
    const result = JSON.parse(reply.response);
    if (!result.ok) throw new Error(result.error || "Note request failed");
    return result;
  }
  /** @param {Snapshot} next */
  function accept(next) {
    if (!next || !Array.isArray(next.notes) || !Number.isInteger(next.revision)) return;
    if (sameMap(snapshot?.map_ref, next.map_ref) && snapshot && next.revision < snapshot.revision) return;
    if (!sameMap(snapshot?.map_ref, next.map_ref)) {
      epoch++;
      selected = null;
      editing = false;
      adding = false;
      busy = false;
    }
    snapshot = next;
    if (selected && !snapshot.notes.some((n) => n.id === selected)) {
      selected = null;
      editing = false;
    }
    render();
    if (!editing && !busy && !panel.hidden) showPanel();
  }
  // Explicit snapshot fetch on reconnect avoids depending on ROS latch delivery order.
  async function reload() {
    if (disposed || reloadPending) return;
    reloadPending = true;
    const started = epoch;
    try {
      const next = await request("snapshot");
      if (!disposed && epoch === started) accept(next);
    } catch {
      if (!disposed && epoch === started) reloadTimer = setTimeout(reload, 3000);
    } finally {
      reloadPending = false;
      if (!disposed && epoch !== started && !snapshot) reloadTimer = setTimeout(reload, 0);
    }
  }
  /** @param {string} text */
  function status(text) {
    let message = panel.querySelector(".map-note-status");
    if (!message) {
      message = el("p", "", "map-note-status");
      message.setAttribute("role", "status");
      panel.append(message);
    }
    message.textContent = text;
  }
  /** @param {string} operation @param {object} args */
  async function mutate(operation, args) {
    if (busy) return;
    busy = true;
    const started = epoch;
    panel.querySelectorAll("button").forEach((b) => (b.disabled = true));
    try {
      const result = await request(operation, args);
      if (disposed || epoch !== started) return;
      selected = result.note?.id || null;
      editing = false;
      accept(result.snapshot);
      showPanel();
    } catch (error) {
      if (!disposed && epoch === started)
        status(`${errorText(error)}. Reopen the note to check its latest state before retrying.`);
    } finally {
      if (!disposed && epoch === started) {
        busy = false;
        panel.querySelectorAll("button").forEach((b) => (b.disabled = false));
      }
    }
  }
  /** @param {Note | null} [note] @param {number[] | null} [point] */
  function showEditor(note = null, point = null) {
    editing = true;
    panel.hidden = false;
    panel.replaceChildren();
    const title = el("input");
    title.value = note?.title || "";
    title.maxLength = 80;
    title.setAttribute("aria-label", "Note title");
    title.placeholder = "Title";
    const text = el("textarea");
    text.value = note?.text || "";
    text.maxLength = 800;
    text.setAttribute("aria-label", "Note text");
    text.placeholder = "What should MARS remember?";
    const certainty = el("select");
    certainty.setAttribute("aria-label", "Certainty");
    for (const value of ["observed", "uncertain"]) {
      const option = el("option", value);
      option.value = value;
      certainty.append(option);
    }
    certainty.value = note?.certainty || "observed";
    panel.append(
      el("h3", note ? "Edit note" : "New map note"),
      title,
      text,
      certainty,
      button("Save note", () => {
        if (!title.value.trim() || !text.value.trim()) {
          status("A title and observation are required.");
          return;
        }
        void mutate("write_map_note", {
          note_id: note?.id ?? null,
          expected_revision: note?.revision ?? null,
          title: title.value,
          text: text.value,
          certainty: certainty.value,
          observation_id: null,
          ...(point ? { map_point: point } : {}),
        });
      }),
      button("Cancel", () => {
        editing = false;
        showPanel();
      }),
    );
    title.focus();
  }
  function showPanel() {
    if (editing) return;
    panel.replaceChildren();
    panel.hidden = false;
    panel.append(
      button("Close", () => {
        panel.hidden = true;
        adding = false;
      }),
    );
    if (!active() || !snapshot) {
      panel.append(el("p", "Notes need a saved map. Waiting for this map's notes."));
      return;
    }
    const note = snapshot.notes.find((n) => n.id === selected);
    if (!note) {
      panel.append(el("h3", `Map notes (${snapshot.notes.length})`));
      panel.append(
        button(visible ? "Hide pins" : "Show pins", () => {
          visible = !visible;
          render();
          showPanel();
        }),
      );
      if (!snapshot.notes.length) panel.append(el("p", "MARS has not written any notes on this map yet."));
      const list = el("div", "", "map-note-list");
      for (const n of snapshot.notes)
        list.append(
          button(n.title, () => {
            selected = n.id;
            showPanel();
            render();
          }),
        );
      panel.append(
        list,
        button("Add note", () => {
          adding = true;
          status("Click a point on the map. Escape cancels.");
        }),
      );
      return;
    }
    panel.append(
      el("h3", note.title),
      el("p", note.text),
      el(
        "p",
        `${note.id} · ${note.anchor === "observation" ? "Seen from here" : "Known map position"} · (${note.x.toFixed(1)}, ${note.y.toFixed(1)}) m`,
        "map-note-meta",
      ),
      el(
        "p",
        `Last seen ${new Date(note.observed_at * 1000).toLocaleString()}${note.certainty === "uncertain" ? " · uncertain" : ""}`,
        "map-note-meta",
      ),
    );
    if (note.has_evidence)
      panel.append(
        button("View captured image", async () => {
          const started = epoch,
            id = selected;
          try {
            const result = await request("read_map_notes", {
              note_ids: [id],
              query: null,
              include_evidence: true,
              limit: 1,
            });
            if (disposed || epoch !== started || selected !== id || editing || panel.hidden) return;
            const image = result.images?.[0];
            if (image) {
              const img = el("img");
              img.alt = "Historical camera observation for this note";
              img.src = `data:image/jpeg;base64,${image.jpeg}`;
              panel.querySelector("img")?.remove();
              panel.append(img);
            } else status("Captured image is unavailable.");
          } catch (error) {
            if (!disposed && epoch === started) status(errorText(error));
          }
        }),
      );
    panel.append(
      button("Edit", () => showEditor(note)),
      button("Remove", () => {
        void mutate("remove_map_note", {
          note_id: note.id,
          expected_revision: note.revision,
        });
      }),
      button("All notes", () => {
        selected = null;
        showPanel();
        render();
      }),
    );
  }
  function render() {
    const usable = active();
    const width = root.clientWidth,
      height = root.clientHeight;
    const key = JSON.stringify([
      usable,
      snapshot?.map_ref,
      snapshot?.revision,
      selected,
      visible,
      geometry.viewKey(),
      width,
      height,
    ]);
    if (key === renderedKey) return;
    renderedKey = key;
    layer.hidden = !visible || !usable;
    toggle.textContent = usable && snapshot ? `Notes ${snapshot.notes.length}` : "Notes";
    if (!usable || !snapshot) {
      layer.replaceChildren();
      pins.clear();
      return;
    }
    const alive = new Set();
    /** @type {Map<string, {button: HTMLButtonElement, count: number}>} */
    const cells = new Map();
    for (const note of snapshot.notes) {
      const p = geometry.project(note.x, note.y);
      if (!p || !Number.isFinite(p.x) || !Number.isFinite(p.y)) continue;
      const key = `${Math.round(p.x / 130)},${Math.round(p.y / 38)}`;
      const cluster = cells.get(key);
      if (cluster && note.id !== selected) {
        cluster.count++;
        cluster.button.textContent = String(cluster.count);
        cluster.button.setAttribute("aria-label", `${cluster.count} notes near this point`);
        cluster.button.onclick = () => {
          selected = null;
          editing = false;
          showPanel();
        };
        continue;
      }
      alive.add(note.id);
      let pin = pins.get(note.id);
      if (!pin) {
        pin = el("button");
        pin.type = "button";
        pin.className = "map-note-pin";
        pins.set(note.id, pin);
        layer.append(pin);
      }
      pin.textContent = note.title.length > 24 ? note.title.slice(0, 23) + "…" : note.title;
      pin.setAttribute("aria-label", note.title);
      pin.setAttribute("aria-pressed", String(note.id === selected));
      pin.dataset.anchor = note.anchor;
      pin.onclick = () => {
        selected = note.id;
        editing = false;
        showPanel();
        render();
      };
      pin.style.left = `${p.x}px`;
      pin.style.top = `${p.y}px`;
      pin.hidden = p.x < 0 || p.y < 0 || p.x > width || p.y > height;
      cells.set(key, { button: pin, count: 1 });
    }
    for (const [id, pin] of pins)
      if (!alive.has(id)) {
        pin.remove();
        pins.delete(id);
      }
  }
  /** @param {PointerEvent} e */
  function mapClick(e) {
    if (!adding || e.target !== geometry.canvas) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    if (e.type !== "pointerup") return;
    adding = false;
    const p = geometry.unproject(e);
    if (p) showEditor(null, [p.x, p.y]);
  }
  /** @param {KeyboardEvent} e */
  function keydown(e) {
    if (e.key === "Escape") {
      adding = false;
      editing = false;
      panel.hidden = true;
    }
  }
  function invalidate() {
    epoch++;
    snapshot = null;
    selected = null;
    editing = false;
    adding = false;
    busy = false;
    panel.hidden = true;
    clearTimeout(reloadTimer);
    render();
    // Deferred so a connection-state notification can clear the widget's old map first.
    reloadTimer = setTimeout(reload, 0);
  }
  root.addEventListener("pointerdown", mapClick, true);
  root.addEventListener("pointerup", mapClick, true);
  root.addEventListener("keydown", keydown);
  const off = ros.subscribe(
    TOPIC,
    (msg) => {
      try {
        accept(JSON.parse(msg.data));
      } catch {
        /* Invalid publication: keep the last valid snapshot. */
      }
    },
    0,
    "std_msgs/msg/String",
  );
  const offConnection = ros.onStateChange(invalidate);
  return {
    render,
    invalidate,
    destroy() {
      disposed = true;
      epoch++;
      clearTimeout(reloadTimer);
      off();
      offConnection();
      root.removeEventListener("pointerdown", mapClick, true);
      root.removeEventListener("pointerup", mapClick, true);
      root.removeEventListener("keydown", keydown);
      layer.remove();
      toggle.remove();
      panel.remove();
    },
  };
}
