// @ts-check
// The Agent panel's compact form: a bottom sheet with three heights — closed
// (its header alone, which is the button that opens it), half, and the whole
// stage. The heights live in CSS, so a resize needs no JS.

// Match --agent-sheet-closed and --agent-sheet-half-min; the drag needs the
// rest heights as numbers to snap to.
const CLOSED_PX = 52;
const HALF_MIN_PX = 320;
const TAP_SLOP_PX = 5;
const STATES = /** @type {const} */ (["closed", "half", "full"]);

/** @typedef {"closed" | "half" | "full"} SheetState */

/**
 * @param {HTMLElement} panel the .agent-panel element
 * @param {{ onOpen?: () => void }} [opts] onOpen fires on leaving the closed
 *   state, so the chat can jump to its newest message.
 * @returns {{
 *   destroy: () => void,
 *   setEnabled: (on: boolean) => void,
 *   open: () => void,
 *   setName: (name: string) => void,
 *   actionSlot: HTMLElement
 * }}
 *   actionSlot is where the panel parks the start/stop button while compact.
 */
export function createAgentSheet(panel, opts = {}) {
  // The header is a row, not a button: it hosts the panel's start/stop, and a
  // button cannot contain another. The grab area beside it takes the taps.
  const header = document.createElement("div");
  header.className = "agent-sheet-header";
  header.hidden = true;
  header.innerHTML =
    '<button type="button" class="agent-sheet-grab">' +
    // Inside the grab so pulling the grip drags; absolute, so still centred.
    '<span class="agent-sheet-grip" aria-hidden="true"></span>' +
    '<svg class="agent-sheet-glyph" viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M12 3.5l1.7 6.8 6.8 1.7-6.8 1.7L12 20.5l-1.7-6.8L3.5 12l6.8-1.7z"/></svg>' +
    '<span class="agent-sheet-name">Agent</span></button>' +
    '<span class="agent-sheet-action"></span>';
  panel.prepend(header);

  const grab = /** @type {HTMLButtonElement} */ (header.querySelector(".agent-sheet-grab"));
  const nameEl = /** @type {HTMLElement} */ (header.querySelector(".agent-sheet-name"));
  const actionSlot = /** @type {HTMLElement} */ (header.querySelector(".agent-sheet-action"));

  /** @type {SheetState} */
  let state = "closed";
  let enabled = false;
  /** @type {{ id: number, y: number, height: number, moved: boolean } | null} */
  let drag = null;
  // A drag ends in a click too; that click must not also toggle the sheet.
  let swallowClick = false;

  /** @param {SheetState} next */
  function setState(next) {
    state = next;
    for (const name of STATES) panel.classList.toggle(`sheet-${name}`, name === next);
    grab.setAttribute("aria-expanded", String(next !== "closed"));
    grab.setAttribute("aria-label", next === "closed" ? "Open chat" : "Close chat");
    if (next !== "closed") opts.onOpen?.();
  }

  const stageHeight = () => panel.parentElement?.clientHeight || window.innerHeight;
  // Mirrors the CSS, including half's floor.
  const snapHeights = () => {
    const full = Math.max(CLOSED_PX, stageHeight() - 28);
    return {
      closed: CLOSED_PX,
      half: Math.min(full, Math.max(HALF_MIN_PX, Math.round(stageHeight() * 0.5))),
      full,
    };
  };

  const onPointerDown = (/** @type {PointerEvent} */ event) => {
    if (!enabled || !event.isPrimary || event.button !== 0) return;
    swallowClick = false;
    drag = { id: event.pointerId, y: event.clientY, height: panel.getBoundingClientRect().height, moved: false };
    grab.setPointerCapture(event.pointerId);
  };

  const onPointerMove = (/** @type {PointerEvent} */ event) => {
    if (!drag || event.pointerId !== drag.id) return;
    const dy = drag.y - event.clientY;
    if (!drag.moved && Math.abs(dy) < TAP_SLOP_PX) return;
    drag.moved = true;
    panel.classList.add("sheet-dragging");
    const snaps = snapHeights();
    panel.style.height = `${Math.min(snaps.full, Math.max(snaps.closed, drag.height + dy))}px`;
  };

  const onPointerUp = (/** @type {PointerEvent} */ event) => {
    if (!drag || event.pointerId !== drag.id) return;
    const dragged = drag.moved;
    const height = panel.getBoundingClientRect().height;
    drag = null;
    panel.classList.remove("sheet-dragging");
    panel.style.height = "";
    if (!dragged) return; // a tap: the click handler owns it
    swallowClick = event.type === "pointerup"; // a cancelled drag sends no click to swallow
    const snaps = snapHeights();
    const nearest = STATES.reduce((best, name) =>
      Math.abs(snaps[name] - height) < Math.abs(snaps[best] - height) ? name : best,
    );
    setState(nearest);
  };

  // Taps land here rather than in pointerup so the keyboard gets them too.
  const onClick = () => {
    if (swallowClick) {
      swallowClick = false;
      return;
    }
    if (enabled) setState(state === "closed" ? "half" : "closed");
  };

  grab.addEventListener("pointerdown", onPointerDown);
  grab.addEventListener("pointermove", onPointerMove);
  grab.addEventListener("pointerup", onPointerUp);
  grab.addEventListener("pointercancel", onPointerUp);
  grab.addEventListener("click", onClick);

  return {
    /** Starting the agent is a request to watch it: show the transcript. */
    open() {
      if (enabled && state === "closed") setState("half");
    },
    setName(name) {
      nameEl.textContent = name;
    },
    actionSlot,
    setEnabled(on) {
      if (on === enabled) return;
      enabled = on;
      header.hidden = !on;
      panel.style.height = "";
      panel.classList.remove("sheet-dragging");
      if (on) {
        setState(state);
        return;
      }
      for (const name of STATES) panel.classList.remove(`sheet-${name}`);
    },
    destroy() {
      header.remove();
    },
  };
}
