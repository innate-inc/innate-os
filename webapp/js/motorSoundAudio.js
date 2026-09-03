// @ts-check
// The physical robot plays its motor voice through ALSA. Simulation publishes
// the identical synth as PCM because its container has no audio device.

import { ros } from "./rosClient.js";
import { PcmAudioPlayer } from "./pcmAudioPlayer.js";
import { isRobotAudioSpeaker } from "./ttsAudio.js";

const TOPIC = "/motor_sound/audio";
let started = false;
/** @type {PcmAudioPlayer | null} */
let player = null;

export function initMotorSoundAudio() {
  if (started) return;
  started = true;

  // Creating/resuming the context inside the operator's first gesture satisfies
  // browser autoplay rules. Until then chunks are deliberately dropped.
  const unlock = () => {
    if (!player) {
      const Context = window.AudioContext || /** @type {any} */ (window).webkitAudioContext;
      if (Context) player = new PcmAudioPlayer(new Context());
    }
    void player?.context.resume();
  };
  window.addEventListener("pointerdown", unlock, { once: true, capture: true });
  window.addEventListener("keydown", unlock, { once: true, capture: true });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "hidden" || !player) return;
    player.reset();
    void player.context.suspend().catch(() => {});
  });

  ros.subscribe(TOPIC, (msg) => {
    if (!player || document.visibilityState === "hidden" || !isRobotAudioSpeaker() || typeof msg?.data !== "string") {
      return;
    }
    let payload;
    try {
      payload = JSON.parse(msg.data);
    } catch {
      // A malformed audio packet should not disrupt the shell.
      return;
    }
    if (player.context.state === "running") {
      player.push(payload);
      return;
    }
    void player.context.resume().then(() => {
      if (document.visibilityState !== "hidden") player?.push(payload);
    }).catch(() => {});
  }, undefined, "std_msgs/msg/String");
}
