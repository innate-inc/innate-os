// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// A presentation-only keepsake. Winning comes exclusively from the world judge.
const ART = "/public/rewards/first-life.png";
const COLLECTION_KEY = "innate.nowhere.first-life.v1";
const ATTEMPT_KEY = "innate.nowhere.first-life.attempt";
const MUTE_KEY = "innate.nowhere.first-life.muted";

/** Storage is optional (private browsing must still let the story finish). */
function read(/** @type {Storage} */ storage, /** @type {string} */ key) {
  try { return storage.getItem(key); } catch { return null; }
}
function write(/** @type {Storage} */ storage, /** @type {string} */ key, /** @type {string} */ value) {
  try { storage.setItem(key, value); } catch { /* a keepsake still lasts for this page */ }
}

/** @param {HTMLElement} root */
export function createPlantReward(root) {
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  const art = new Image();
  art.src = ART;
  const decoded = art.decode().catch(() => {});
  let collected = read(localStorage, COLLECTION_KEY) === "1";
  let muted = read(localStorage, MUTE_KEY) === "1";
  let generation = 0;
  let raf = 0;
  /** @type {ReturnType<typeof setTimeout>[]} */ let timers = [];
  /** @type {AudioContext | null} */ let audio = null;
  /** @type {GainNode | null} */ let master = null;
  /** @type {HTMLDialogElement | null} */ let dialog = null;
  /** @type {HTMLElement | null} */ let previousFocus = null;
  /** @type {(() => void) | null} */ let collect = null;
  /** @type {string} */ let lastAttempt = read(sessionStorage, ATTEMPT_KEY) || "";

  const keepsake = document.createElement("button");
  keepsake.className = "plant-keepsake";
  keepsake.innerHTML = `<img src="${ART}" alt=""><span><small>FIRST LIFE</small>A little hope</span><span aria-hidden="true">↗</span>`;
  keepsake.setAttribute("aria-label", "First life collected. See your plant again");
  keepsake.hidden = !collected;
  root.append(keepsake);
  keepsake.onclick = () => play("", () => {}, true);

  // Unlock only on a real user gesture, well before the world announces a win.
  function unlock() {
    try {
      audio ??= new AudioContext();
      void audio.resume().catch(() => {});
    } catch { /* The visual sequence is complete without audio. */ }
  }
  document.addEventListener("pointerdown", unlock, { once: true });
  document.addEventListener("keydown", unlock, { once: true });

  /** Original rising celesta phrase, then a warm major-ninth bloom. No audio download. */
  function fanfare() {
    if (!audio || audio.state !== "running" || muted) return;
    master?.disconnect();
    master = audio.createGain();
    master.gain.value = 0.4;
    master.connect(audio.destination);
    const now = audio.currentTime;
    function note(/** @type {number} */ midi, /** @type {number} */ when, /** @type {number} */ duration, /** @type {number} */ level) {
      if (!audio || !master) return;
      const hz = 440 * 2 ** ((midi - 69) / 12);
      for (const [harmonic, amplitude] of [[1, 1], [2, 0.24], [3, 0.07]]) {
        const oscillator = audio.createOscillator();
        const gain = audio.createGain();
        oscillator.frequency.value = hz * harmonic;
        gain.gain.setValueAtTime(0, now + when);
        gain.gain.linearRampToValueAtTime(level * amplitude, now + when + 0.018);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + when + duration);
        oscillator.connect(gain).connect(master);
        oscillator.start(now + when);
        oscillator.stop(now + when + duration + 0.02);
        oscillator.onended = () => { oscillator.disconnect(); gain.disconnect(); };
      }
    }
    [60, 67, 72, 76, 79, 84].forEach((n, i) => note(n, 0.25 + i * 0.19, 1.6, 0.07));
    [48, 60, 64, 67, 74, 79].forEach((n) => note(n, 1.65, 3.6, 0.065));
    [84, 86, 91, 88].forEach((n, i) => note(n, 2.15 + i * 0.21, 2.2, 0.04));
  }

  function collectionChime() {
    if (!audio || audio.state !== "running" || muted) return;
    master?.disconnect();
    master = audio.createGain();
    master.gain.value = 0.16;
    master.connect(audio.destination);
    const now = audio.currentTime;
    [1046.5, 1318.51, 1567.98].forEach((hz, i) => {
      if (!audio || !master) return;
      const oscillator = audio.createOscillator();
      const gain = audio.createGain();
      const at = now + i * 0.075;
      oscillator.frequency.value = hz;
      gain.gain.setValueAtTime(0, at);
      gain.gain.linearRampToValueAtTime(0.45, at + 0.012);
      gain.gain.exponentialRampToValueAtTime(0.0001, at + 0.55);
      oscillator.connect(gain).connect(master);
      oscillator.start(at);
      oscillator.stop(at + 0.56);
      oscillator.onended = () => { oscillator.disconnect(); gain.disconnect(); };
    });
  }

  function cancel() {
    generation++;
    timers.forEach(clearTimeout);
    timers = [];
    cancelAnimationFrame(raf);
    master?.disconnect();
    master = null;
    if (dialog) { dialog.close(); dialog.remove(); dialog = null; }
    if (previousFocus?.isConnected) previousFocus.focus({ preventScroll: true });
    previousFocus = null;
    collect = null;
    keepsake.hidden = !collected;
  }
  function later(/** @type {() => void} */ fn, /** @type {number} */ ms) { timers.push(setTimeout(fn, ms)); }

  /** @param {string} attempt @param {() => void} onCollect @param {boolean} [replay] */
  function play(attempt, onCollect, replay = false) {
    cancel();
    if (!replay && attempt && lastAttempt === attempt) { onCollect(); return; }
    const ownGeneration = generation;
    previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    keepsake.hidden = true;
    const view = document.createElement("dialog");
    dialog = view;
    view.className = "plant-reward";
    if (root.classList.contains("agent-cockpit")) view.style.setProperty("--plant-flight-x", "-38vw");
    view.setAttribute("aria-labelledby", "plant-reward-title");
    view.setAttribute("aria-describedby", "plant-reward-description");
    view.innerHTML = `
      <div class="plant-reward-sky" aria-hidden="true"></div>
      <canvas class="plant-reward-dust" aria-hidden="true"></canvas>
      <button class="plant-reward-sound" type="button"></button>
      <div class="plant-reward-content">
        <p class="plant-reward-eyebrow">THE BACKROOMS · COMPLETE</p>
        <div class="plant-reward-stage" role="img" aria-label="A tiny green plant growing in an old leather boot">
          <div class="plant-reward-halo"></div><div class="plant-reward-ring"></div>
          <div class="plant-reward-orbit"></div><div class="plant-reward-orbit second"></div>
          <div class="plant-reward-lift"><img class="plant-reward-boot" src="${ART}" alt=""></div>
          <span class="plant-reward-spark one">✦</span><span class="plant-reward-spark two">✦</span><span class="plant-reward-spark three">✧</span>
        </div>
        <div class="plant-reward-copy">
          <p class="plant-reward-label">A LITTLE HOPE, FOUND.</p>
          <h1 id="plant-reward-title">You found life.</h1>
          <p id="plant-reward-description">In a place that went on forever,<br>you found something worth bringing home.</p>
        </div>
        <div class="plant-reward-actions">
          <button class="plant-reward-take" type="button">${replay ? "Keep exploring" : "Take it with you"}<span aria-hidden="true">↗</span></button>
          <p>A small plant. A big beginning.</p>
        </div>
      </div>`;
    const sound = /** @type {HTMLButtonElement} */ (view.querySelector(".plant-reward-sound"));
    const take = /** @type {HTMLButtonElement} */ (view.querySelector(".plant-reward-take"));
    const soundLabel = () => {
      const on = !muted && audio?.state === "running";
      sound.textContent = on ? "♪ Sound on" : "♪ Sound off";
      sound.setAttribute("aria-label", on ? "Mute celebration" : "Enable celebration sound");
      sound.setAttribute("aria-pressed", String(!!on));
    };
    soundLabel();
    sound.onclick = () => {
      muted = !muted && audio?.state === "running";
      write(localStorage, MUTE_KEY, muted ? "1" : "0");
      if (muted) { master?.disconnect(); master = null; }
      else { unlock(); void audio?.resume().then(() => { if (ownGeneration !== generation) return; soundLabel(); if (!view.classList.contains("collecting")) fanfare(); }).catch(() => {}); }
      soundLabel();
    };
    let taking = false;
    collect = () => {
      if (taking) return;
      taking = true;
      collectionChime();
      collected = true;
      if (attempt) { lastAttempt = attempt; write(sessionStorage, ATTEMPT_KEY, attempt); }
      write(localStorage, COLLECTION_KEY, "1");
      view.classList.add("collecting");
      take.textContent = "Yours. Let's go.";
      // Leave the final pose long enough for the collection to register.
      later(() => { cancel(); onCollect(); }, reduced.matches ? 0 : 850);
    };
    take.onclick = () => collect?.();
    view.addEventListener("cancel", (event) => { event.preventDefault(); collect?.(); });
    document.body.append(view);
    view.showModal();
    sound.focus({ preventScroll: true });
    // A slow image never misses its own reveal. A failed image still offers a way onward.
    void Promise.race([decoded, new Promise((resolve) => later(() => resolve(undefined), 1800))]).then(() => {
      if (ownGeneration !== generation) return;
      view.classList.add("playing");
      if (!reduced.matches) particles(view);
      fanfare();
      soundLabel();
      later(() => { view.classList.add("ready"); take.focus({ preventScroll: true }); }, reduced.matches ? 0 : 3600);
    });
  }

  /** One bounded canvas, no particle DOM, capped resolution; stops on close/unmount. */
  function particles(/** @type {HTMLDialogElement} */ view) {
    const canvas = /** @type {HTMLCanvasElement} */ (view.querySelector("canvas"));
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const start = performance.now();
    const motes = Array.from({ length: 76 }, (_, i) => ({
      angle: i * 2.39996, distance: 0.16 + (i % 13) / 18, size: 0.7 + (i % 4) * 0.55,
    }));
    function frame(/** @type {number} */ now) {
      if (!ctx) return;
      const w = view.clientWidth, h = view.clientHeight;
      const dpr = Math.min(devicePixelRatio || 1, 1.5);
      if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
        canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      const t = (now - start) / 1000;
      const burst = Math.max(0, t - 1.65);
      const expansion = 1 - Math.exp(-burst * 1.8);
      for (const [i, p] of motes.entries()) {
        const r = Math.min(w, h) * p.distance * expansion;
        const x = w / 2 + Math.cos(p.angle + burst * 0.025) * r;
        const y = h * 0.42 + Math.sin(p.angle + burst * 0.025) * r * 0.72 - burst * (2 + i % 3);
        ctx.globalAlpha = Math.min(1, burst * 3) * (0.3 + 0.5 * Math.sin(t * 1.7 + i) ** 2);
        ctx.fillStyle = i % 3 ? "#f5dda4" : "#b4f5c4";
        ctx.beginPath(); ctx.arc(x, y, p.size, 0, Math.PI * 2); ctx.fill();
      }
      raf = requestAnimationFrame(frame);
    }
    raf = requestAnimationFrame(frame);
  }
  return {
    play, cancel,
    /** The studio docks the keepsake with the agent on desktop, on the stage on phones.
     * @param {HTMLElement} host */
    mountKeepsake(host) { host.append(keepsake); },
    destroy() {
      cancel();
      keepsake.remove();
      document.removeEventListener("pointerdown", unlock);
      document.removeEventListener("keydown", unlock);
      void audio?.close().catch(() => {});
    },
  };
}
