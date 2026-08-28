// @ts-check
// Operator mic button with the ripple/waveform glass the agent composer
// introduced. Shared by the two pages that capture the operator's voice, in
// two modes: "hold" (push-to-talk — pointer-hold or spacebar) on the agent
// page (sim), where speech becomes a transcript, and "toggle" (Meet-style
// mute/unmute — a click or a spacebar tap) on teleop, where it plays live out
// the robot's speaker. In both modes the spacebar belongs to whatever the
// operator is typing in.

import { isTypingContext } from "./shell.js";

const RIPPLE_COUNT = 3;
const WAVEFORM_BAR_COUNT = 9;
const POINTER_HOLD_DELAY_MS = 350;
const SPACEBAR_KEY_CODE = "Space";
const SPACEBAR_HOLD_DELAY_MS = 180;
const HOLD_HINT_DURATION_MS = 4200;
/** Mic RMS is quiet; scale it so the resting glass glow reads as activity. */
const LEVEL_GAIN = 6;
/** Idle bar height so the waveform never collapses to a flat line. */
const WAVEFORM_FLOOR = 0.12;
const WAVEFORM_DB_RANGE = 48;

/**
 * @param {HTMLElement} root
 * @param {{
 *   startListening: () => void | Promise<void>,
 *   stopListening: () => void,
 *   mode?: "hold" | "toggle",
 *   holdLabel?: string,
 *   listeningLabel?: string,
 *   buttonLabel?: string,
 *   buttonHint?: string,
 *   activeButtonLabel?: string,
 *   activeButtonHint?: string,
 *   composerInput?: Element | null
 * }} callbacks
 * @returns {{
 *   destroy: () => void,
 *   setEnabled: (enabled: boolean) => void,
 *   setCaptureState: (state: { on: boolean, busy: boolean, error: string | null }) => void,
 *   setReceiveState: (state: { on: boolean, label?: string, hint?: string }) => void,
 *   setAudioFeedback: (feedback: { level: number, waveform: number[] }) => void
 * }}
 */
export function createMicControl(root, callbacks) {
  const {
    startListening,
    stopListening,
    mode = "hold",
    holdLabel = "Hold to talk to the agent",
    listeningLabel = "Listening…",
    buttonLabel = "",
    buttonHint = "",
    activeButtonLabel = "LIVE",
    activeButtonHint = "Release to stop",
    composerInput = null,
  } = callbacks;
  const isToggle = mode === "toggle";
  const idleTitle = isToggle
    ? "Toggle your mic — Spacebar or click"
    : "Hold to talk — Spacebar, or click and hold";

  const control = document.createElement("div");
  control.className = "agent-mic-control";

  const button = document.createElement("button");
  button.type = "button";
  button.className = "agent-mic-button";
  button.setAttribute("aria-label", holdLabel);
  button.setAttribute("aria-pressed", "false");
  button.title = idleTitle;

  const icon = decorativeSpan("agent-mic-icon");
  const action = document.createElement("span");
  action.className = "agent-mic-action";
  const actionLabel = document.createElement("span");
  actionLabel.className = "agent-mic-action-label";
  const actionHint = document.createElement("span");
  actionHint.className = "agent-mic-action-hint";
  action.append(actionLabel, actionHint);

  const waveform = decorativeSpan("agent-mic-waveform");
  const waveformBars = Array.from({ length: WAVEFORM_BAR_COUNT }, () =>
    decorativeSpan("agent-mic-waveform-bar"),
  );
  waveform.append(...waveformBars);

  const innerWaves = rippleGroup("agent-mic-inner-waves", "agent-mic-inner-wave");
  const hoverGlow = decorativeSpan("agent-mic-hover-glow");
  const outerWaves = rippleGroup("agent-mic-outer-waves", "agent-mic-outer-wave");

  const stateLabel = document.createElement("span");
  stateLabel.className = "agent-mic-label";
  stateLabel.setAttribute("role", "status");
  stateLabel.setAttribute("aria-live", "polite");

  const messageBubble = document.createElement("span");
  messageBubble.className = "agent-mic-message";
  messageBubble.setAttribute("role", "status");
  messageBubble.setAttribute("aria-hidden", "true");

  button.append(innerWaves, hoverGlow, icon, waveform);
  if (buttonLabel) {
    button.classList.add("labeled");
    button.append(action);
  }
  control.append(outerWaves, button, stateLabel, messageBubble);
  root.append(control);

  /** @type {Set<"pointer" | "spacebar">} */
  const activeHoldSources = new Set();
  const eventListenerController = new AbortController();
  let isPushToTalkActive = false;
  let isCaptureOn = false;
  let isCaptureBusy = false;
  let isMicEnabled = true;
  let isReceiveOn = false;
  let receiveLabel = "LISTENING";
  let receiveHint = "3s after quiet";
  /** @type {string | null} */
  let unavailableReason = null;
  let listeningRequestId = 0;
  let pointerHoldTimeout = 0;
  let spacebarHoldTimeout = 0;
  let isHoldHintVisible = false;
  let isUnavailableMessageVisible = false;
  let holdHintTimeout = 0;

  function renderMicState() {
    const isUnavailable = unavailableReason !== null;
    const isListening = isPushToTalkActive && isCaptureOn && !isUnavailable;
    const isWaiting =
      isPushToTalkActive && (isCaptureBusy || !isCaptureOn) && !isUnavailable;
    const isReceiving = isReceiveOn && !isWaiting && !isListening && !isUnavailable;
    const shouldShowHoldHint =
      isHoldHintVisible && !isUnavailable && !isWaiting && !isListening;
    const shouldShowUnavailableMessage = isUnavailable && isUnavailableMessageVisible;
    control.classList.toggle("listening", isListening);
    control.classList.toggle("receiving", isReceiving);
    control.classList.toggle("waiting", isWaiting);
    control.classList.toggle("unavailable", isUnavailable);
    control.classList.toggle("show-hold-hint", shouldShowHoldHint);
    control.classList.toggle("show-unavailable-message", shouldShowUnavailableMessage);
    button.setAttribute("aria-pressed", String(isPushToTalkActive));
    button.setAttribute("aria-busy", String(isWaiting));
    button.setAttribute(
      "aria-label",
      isUnavailable ? unavailableReason || "Microphone unavailable" : holdLabel,
    );
    button.title = isUnavailable ? "" : idleTitle;
    let statusText = "";
    let visibleLabel = buttonLabel;
    let visibleHint = buttonHint;
    if (isWaiting) {
      statusText = "Starting…";
      visibleLabel = "CONNECTING";
      visibleHint = isToggle ? "One moment" : "Keep holding";
    } else if (isListening) {
      statusText = listeningLabel;
      visibleLabel = activeButtonLabel;
      visibleHint = activeButtonHint;
    } else if (isReceiving) {
      statusText = `${receiveLabel} — ${receiveHint}`;
      visibleLabel = receiveLabel;
      visibleHint = receiveHint;
    }
    stateLabel.textContent = statusText;
    actionLabel.textContent = visibleLabel;
    actionHint.textContent = visibleHint;
    const messageText = shouldShowUnavailableMessage
      ? unavailableReason || "Microphone unavailable"
      : shouldShowHoldHint
        ? "Hold down your spacebar or mouse to talk"
        : "";
    renderMessageBubble(messageText);
  }

  /** @param {string} text */
  function renderMessageBubble(text) {
    if (!text) {
      messageBubble.setAttribute("aria-hidden", "true");
      return;
    }
    messageBubble.textContent = text;
    messageBubble.removeAttribute("aria-hidden");
  }

  function dismissHoldHint() {
    window.clearTimeout(holdHintTimeout);
    isHoldHintVisible = false;
  }

  function showHoldHint() {
    dismissHoldHint();
    isHoldHintVisible = true;
    holdHintTimeout = window.setTimeout(() => {
      isHoldHintVisible = false;
      renderMicState();
    }, HOLD_HINT_DURATION_MS);
    renderMicState();
  }

  function dismissMicMessages() {
    if (!isHoldHintVisible && !isUnavailableMessageVisible) return;
    dismissHoldHint();
    isUnavailableMessageVisible = false;
    renderMicState();
  }

  function updateMicAvailability() {
    const wasDisabled = button.disabled;
    button.disabled = !isMicEnabled || unavailableReason !== null;
    if (button.disabled && !wasDisabled) releaseAllPushToTalkHolds();
    renderMicState();
  }

  async function startPushToTalk() {
    if (button.disabled || isPushToTalkActive) return;
    isPushToTalkActive = true;
    const requestId = ++listeningRequestId;
    renderMicState();
    try {
      await startListening();
      // release may land while permission or agent startup is pending
      if (requestId !== listeningRequestId && !isPushToTalkActive) stopListening();
    } catch {
      if (requestId !== listeningRequestId) return;
      activeHoldSources.clear();
      isPushToTalkActive = false;
      renderMicState();
    }
  }

  function stopPushToTalk() {
    if (!isPushToTalkActive) return;
    isPushToTalkActive = false;
    listeningRequestId++;
    renderMicState();
    stopListening();
  }

  function syncPushToTalk() {
    if (activeHoldSources.size > 0) void startPushToTalk();
    else stopPushToTalk();
  }

  function toggleMic() {
    if (isPushToTalkActive) stopPushToTalk();
    else void startPushToTalk();
  }

  /** @param {KeyboardEvent} event */
  function onToggleKeyDown(event) {
    if (event.code !== SPACEBAR_KEY_CODE) return;
    if (
      button.disabled ||
      event.metaKey ||
      event.ctrlKey ||
      event.altKey ||
      event.shiftKey ||
      !spacebarCanControlMic()
    ) {
      return;
    }
    // Every repeat too, or a held spacebar scrolls the page; it also keeps a
    // focused button from firing its native click on keyup (a double toggle).
    event.preventDefault();
    if (!event.repeat) toggleMic();
  }

  function cancelPendingPointerHold() {
    const pending = pointerHoldTimeout !== 0;
    window.clearTimeout(pointerHoldTimeout);
    pointerHoldTimeout = 0;
    return pending;
  }

  function cancelPendingSpacebarHold() {
    const pending = spacebarHoldTimeout !== 0;
    window.clearTimeout(spacebarHoldTimeout);
    spacebarHoldTimeout = 0;
    return pending;
  }

  function releaseAllPushToTalkHolds() {
    cancelPendingPointerHold();
    cancelPendingSpacebarHold();
    activeHoldSources.clear();
    syncPushToTalk();
  }

  /** @param {PointerEvent} event */
  function onMicPointerDown(event) {
    if (event.button !== 0) return;
    event.preventDefault();
    dismissHoldHint();
    renderMicState();
    button.setPointerCapture(event.pointerId);
    cancelPendingPointerHold();
    pointerHoldTimeout = window.setTimeout(() => {
      pointerHoldTimeout = 0;
      activeHoldSources.add("pointer");
      syncPushToTalk();
    }, POINTER_HOLD_DELAY_MS);
  }

  function onMicPointerUp() {
    const wasQuickClick = cancelPendingPointerHold();
    activeHoldSources.delete("pointer");
    syncPushToTalk();
    if (wasQuickClick && !button.disabled) showHoldHint();
  }

  function onMicPointerCancel() {
    cancelPendingPointerHold();
    activeHoldSources.delete("pointer");
    syncPushToTalk();
  }

  function spacebarCanControlMic() {
    // Only a focused composer holding a draft blocks the spacebar; an unsent
    // draft must not disable push-to-talk for the whole page. Pages without a
    // composer (teleop) fall through to the generic typing guard.
    if (composerInput instanceof HTMLTextAreaElement && document.activeElement === composerInput) {
      return composerInput.value.length === 0;
    }
    return !isTypingContext();
  }

  /** @param {KeyboardEvent} event */
  function onWindowKeyDown(event) {
    if (event.code !== SPACEBAR_KEY_CODE) {
      cancelPendingSpacebarHold();
      return;
    }
    if (event.repeat) {
      if (spacebarHoldTimeout !== 0 || activeHoldSources.has("spacebar")) {
        event.preventDefault();
      }
      return;
    }
    if (
      button.disabled ||
      event.metaKey ||
      event.ctrlKey ||
      event.altKey ||
      event.shiftKey ||
      !spacebarCanControlMic()
    ) {
      return;
    }
    event.preventDefault();
    cancelPendingSpacebarHold();
    spacebarHoldTimeout = window.setTimeout(() => {
      spacebarHoldTimeout = 0;
      if (!spacebarCanControlMic()) return;
      activeHoldSources.add("spacebar");
      syncPushToTalk();
    }, SPACEBAR_HOLD_DELAY_MS);
  }

  /** @param {KeyboardEvent} event */
  function onWindowKeyUp(event) {
    if (event.code !== SPACEBAR_KEY_CODE) return;
    if (cancelPendingSpacebarHold()) {
      event.preventDefault();
      showHoldHint();
      return;
    }
    if (!activeHoldSources.has("spacebar")) return;
    event.preventDefault();
    activeHoldSources.delete("spacebar");
    syncPushToTalk();
  }

  function onDocumentVisibilityChange() {
    if (document.visibilityState === "hidden") releaseAllPushToTalkHolds();
  }

  function onDocumentFocusIn() {
    if (isTypingContext() && activeHoldSources.delete("spacebar")) {
      syncPushToTalk();
    }
  }

  const listenerOptions = { signal: eventListenerController.signal };
  if (isToggle) {
    button.addEventListener("click", toggleMic, listenerOptions);
    window.addEventListener("keydown", onToggleKeyDown, listenerOptions);
  } else {
    button.addEventListener("pointerdown", onMicPointerDown, listenerOptions);
    button.addEventListener("pointerup", onMicPointerUp, listenerOptions);
    button.addEventListener("pointercancel", onMicPointerCancel, listenerOptions);
    button.addEventListener("lostpointercapture", onMicPointerCancel, listenerOptions);
    window.addEventListener("keydown", onWindowKeyDown, listenerOptions);
    window.addEventListener("keyup", onWindowKeyUp, listenerOptions);
    window.addEventListener("blur", releaseAllPushToTalkHolds, listenerOptions);
    document.addEventListener("focusin", onDocumentFocusIn, listenerOptions);
  }
  // A hidden tab must not keep a live mic open (toggle) or a stuck hold (hold).
  document.addEventListener("visibilitychange", onDocumentVisibilityChange, listenerOptions);
  document.addEventListener("pointerdown", dismissMicMessages, listenerOptions);
  renderMicState();

  return {
    setEnabled(enabled) {
      if (isMicEnabled === enabled) return;
      isMicEnabled = enabled;
      updateMicAvailability();
    },
    setCaptureState({ on, busy, error }) {
      if (isCaptureOn === on && isCaptureBusy === busy && unavailableReason === error) return;
      const errorChanged = unavailableReason !== error;
      isCaptureOn = on;
      isCaptureBusy = busy;
      unavailableReason = error;
      if (errorChanged) {
        dismissHoldHint();
        isUnavailableMessageVisible = error !== null;
      }
      updateMicAvailability();
    },
    setReceiveState({ on, label = "LISTENING", hint = "3s after quiet" }) {
      if (isReceiveOn === on && receiveLabel === label && receiveHint === hint) return;
      isReceiveOn = on;
      receiveLabel = label;
      receiveHint = hint;
      renderMicState();
    },
    /** @param {{ level: number, waveform: number[] }} feedback */
    setAudioFeedback({ level, waveform }) {
      control.style.setProperty("--agent-mic-level", String(clamp(level * LEVEL_GAIN)));
      waveformBars.forEach((bar, index) => {
        const amplitude = waveformHeight(waveform[index] ?? 0);
        bar.style.setProperty("--agent-wave", String(amplitude));
      });
    },
    destroy() {
      dismissHoldHint();
      releaseAllPushToTalkHolds();
      eventListenerController.abort();
      control.remove();
    },
  };
}

/** @param {string} className */
function decorativeSpan(className) {
  const element = document.createElement("span");
  element.className = className;
  element.setAttribute("aria-hidden", "true");
  return element;
}

/** @param {string} groupClass @param {string} rippleClass */
function rippleGroup(groupClass, rippleClass) {
  const group = decorativeSpan(groupClass);
  const ripples = Array.from({ length: RIPPLE_COUNT }, () => decorativeSpan(rippleClass));
  group.append(...ripples);
  return group;
}

/** @param {number} value @param {number} [min] @returns {number} */
function clamp(value, min = 0) {
  return Math.max(min, Math.min(1, value));
}

/** @param {number} amplitude @returns {number} */
function waveformHeight(amplitude) {
  if (amplitude <= 0) return WAVEFORM_FLOOR;
  const decibels = 20 * Math.log10(amplitude);
  return clamp((decibels + WAVEFORM_DB_RANGE) / WAVEFORM_DB_RANGE, WAVEFORM_FLOOR);
}
