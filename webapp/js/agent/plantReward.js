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
  try {
    return storage.getItem(key);
  } catch {
    return null;
  }
}
function write(/** @type {Storage} */ storage, /** @type {string} */ key, /** @type {string} */ value) {
  try {
    storage.setItem(key, value);
  } catch {
    /* a keepsake still lasts for this page */
  }
}

/** @param {HTMLElement} root */
export function createPlantReward(root) {
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");

  let collected = read(localStorage, COLLECTION_KEY) === "1";
  let muted = read(localStorage, MUTE_KEY) === "1";
  let generation = 0;
  /** @type {{ ready: Promise<void>, start: () => void, collect: () => void, dispose: () => void } | null} */ let scene =
    null;
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
    } catch {
      /* The visual sequence is complete without audio. */
    }
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
    function note(
      /** @type {number} */ midi,
      /** @type {number} */ when,
      /** @type {number} */ duration,
      /** @type {number} */ level,
    ) {
      if (!audio || !master) return;
      const hz = 440 * 2 ** ((midi - 69) / 12);
      for (const [harmonic, amplitude] of [
        [1, 1],
        [2, 0.24],
        [3, 0.07],
      ]) {
        const oscillator = audio.createOscillator();
        const gain = audio.createGain();
        oscillator.frequency.value = hz * harmonic;
        gain.gain.setValueAtTime(0, now + when);
        gain.gain.linearRampToValueAtTime(level * amplitude, now + when + 0.018);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + when + duration);
        oscillator.connect(gain).connect(master);
        oscillator.start(now + when);
        oscillator.stop(now + when + duration + 0.02);
        oscillator.onended = () => {
          oscillator.disconnect();
          gain.disconnect();
        };
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
      oscillator.onended = () => {
        oscillator.disconnect();
        gain.disconnect();
      };
    });
  }

  function cancel() {
    generation++;
    timers.forEach(clearTimeout);
    timers = [];
    scene?.dispose();
    scene = null;
    master?.disconnect();
    master = null;
    if (dialog) {
      dialog.close();
      dialog.remove();
      dialog = null;
    }
    if (previousFocus?.isConnected) previousFocus.focus({ preventScroll: true });
    previousFocus = null;
    collect = null;
    keepsake.hidden = !collected;
  }
  function later(/** @type {() => void} */ fn, /** @type {number} */ ms) {
    const timer = setTimeout(fn, ms);
    timers.push(timer);
    return timer;
  }

  /** @param {string} attempt @param {() => void} onCollect @param {boolean} [replay] */
  function play(attempt, onCollect, replay = false) {
    cancel();
    if (!replay && attempt && lastAttempt === attempt) {
      onCollect();
      return;
    }
    const ownGeneration = generation;
    previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    keepsake.hidden = true;
    const view = document.createElement("dialog");
    dialog = view;
    view.className = "plant-reward";
    view.setAttribute("aria-labelledby", "plant-reward-title");
    view.setAttribute("aria-describedby", "plant-reward-description");
    view.innerHTML = `
      <div class="plant-reward-sky" aria-hidden="true"></div>
      <button class="plant-reward-sound" type="button"></button>
      <div class="plant-reward-content">
        <p class="plant-reward-eyebrow">THE BACKROOMS · COMPLETE</p>
        <div class="plant-reward-stage">
          <p class="plant-reward-orbit-hint">Drag to look around · arrow keys to orbit</p>
          <p class="plant-reward-loading" role="status">Bringing MARS into the spotlight…</p>
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
      if (muted) {
        master?.disconnect();
        master = null;
      } else {
        unlock();
        void audio
          ?.resume()
          .then(() => {
            if (ownGeneration !== generation) return;
            soundLabel();
            if (!view.classList.contains("collecting")) fanfare();
          })
          .catch(() => {});
      }
      soundLabel();
    };
    let taking = false;
    collect = () => {
      if (taking) return;
      taking = true;
      collectionChime();
      scene?.collect();
      collected = true;
      if (attempt) {
        lastAttempt = attempt;
        write(sessionStorage, ATTEMPT_KEY, attempt);
      }
      write(localStorage, COLLECTION_KEY, "1");
      view.classList.add("collecting");
      take.textContent = "Yours. Let's go.";
      // Leave the final pose long enough for the collection to register.
      later(
        () => {
          cancel();
          onCollect();
        },
        reduced.matches ? 0 : 850,
      );
    };
    take.onclick = () => collect?.();
    view.addEventListener("cancel", (event) => {
      event.preventDefault();
      collect?.();
    });
    document.body.append(view);
    view.showModal();
    sound.focus({ preventScroll: true });
    const stage = /** @type {HTMLElement} */ (view.querySelector(".plant-reward-stage"));
    const loading = /** @type {HTMLElement} */ (view.querySelector(".plant-reward-loading"));
    let expired = false;
    const ready = (async () => {
      // The existing sim bundle supplies both Three.js and the real MARS model loader.
      // @ts-ignore -- runtime URL served by the robot's proxy, not a webapp module.
      const mod = await import("/sim-viewer/sim-session.js");
      if (ownGeneration !== generation || expired) return;
      scene = mod.createPlantRewardScene(stage);
      await scene?.ready;
    })();
    let loadTimer = 0;
    const timeout = new Promise((_, reject) => {
      loadTimer = later(() => {
        expired = true;
        reject(new Error("The 3D celebration took too long to load."));
      }, 20_000);
    });
    void Promise.race([ready, timeout])
      .then(() => {
        clearTimeout(loadTimer);
        if (ownGeneration !== generation || expired || taking) return;
        loading.remove();
        scene?.start();
        view.classList.add("playing");
        fanfare();
        soundLabel();
        later(
          () => {
            view.classList.add("ready");
            take.focus({ preventScroll: true });
          },
          reduced.matches ? 0 : 3600,
        );
      })
      .catch((error) => {
        clearTimeout(loadTimer);
        if (ownGeneration !== generation) return;
        scene?.dispose();
        scene = null;
        console.warn("[plant-reward] 3D scene unavailable:", error);
        loading.textContent = "The 3D scene couldn't load. Your plant is still yours to keep.";
        view.classList.add("playing", "ready");
        take.focus({ preventScroll: true });
      });
  }

  return {
    play,
    cancel,
    /** The studio docks the keepsake with the agent on desktop, on the stage on phones.
     * @param {HTMLElement} host */
    mountKeepsake(host) {
      host.append(keepsake);
    },
    destroy() {
      cancel();
      keepsake.remove();
      document.removeEventListener("pointerdown", unlock);
      document.removeEventListener("keydown", unlock);
      void audio?.close().catch(() => {});
    },
  };
}
