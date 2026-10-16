// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// One demonstration-conditioned run, assembled from /brain/icl_trace.
//
// The topic is a flat event stream (see ICL_TRACE_TOPIC in constants.js); this
// turns it into the model a view can render: the run header, the phase map, and
// an ordered feed where a step's `execution` merges into the step it belongs to
// rather than arriving as a second row. Every event repeats the header, so a
// page opened mid-run rebuilds from whichever message lands first.

import { ICL_TRACE_TOPIC } from "../constants.js";

// Frames the model saw, kept for the most recent turns only: each step carries
// two base64 JPEGs (~120 KB), so an unbounded feed would grow past 20 MB on a
// long run. Older rows keep their reasoning and drop their pictures.
const IMAGE_TAIL = 12;
const FEED_MAX = 240;

/** @typedef {{ kind: string, step?: number, t: number, [k: string]: any }} Entry */

/**
 * @param {import("../rosClient.js").RosClient} ros
 * @param {(run: any) => void} onChange
 * @returns {{ run: () => any, destroy: () => void }}
 */
export function createTraceStream(ros, onChange) {
  /** @type {any} */
  let run = emptyRun();

  function emptyRun() {
    return {
      id: "",
      skill: "",
      model: "",
      demonstration: "",
      episodeFrames: 0,
      overview: [],
      phases: [],
      phase: 0,
      active: false,
      ended: null,
      startedAt: 0,
      feed: /** @type {Entry[]} */ ([]),
    };
  }

  /** @param {Entry} entry */
  function push(entry) {
    run.feed.push(entry);
    // Drop pictures from all but the newest turns before trimming the feed.
    const steps = run.feed.filter((/** @type {Entry} */ e) => e.kind === "step");
    for (const old of steps.slice(0, Math.max(0, steps.length - IMAGE_TAIL))) old.images = null;
    if (run.feed.length > FEED_MAX) run.feed.splice(0, run.feed.length - FEED_MAX);
  }

  /** @param {any} ev */
  function apply(ev) {
    // A run id we haven't seen supersedes whatever was on screen: one page, one run.
    if (ev.run && ev.run !== run.id) {
      run = emptyRun();
      run.id = ev.run;
      run.startedAt = ev.t ?? 0;
    }
    run.skill = ev.skill ?? run.skill;
    run.model = ev.model ?? run.model;
    run.demonstration = ev.demonstration ?? run.demonstration;
    run.episodeFrames = ev.episode_frames ?? run.episodeFrames;
    if (Array.isArray(ev.overview)) run.overview = ev.overview;
    if (Array.isArray(ev.phases) && ev.phases.length) run.phases = ev.phases;

    if (ev.ev === "run") {
      run.active = ev.state === "start";
      run.ended = ev.state === "end" ? { ok: !!ev.ok, cancelled: !!ev.cancelled, message: ev.message ?? "" } : null;
      if (ev.state === "start") push({ kind: "start", t: ev.t });
      else push({ kind: "end", t: ev.t, ok: !!ev.ok, cancelled: !!ev.cancelled, message: ev.message ?? "" });
      return;
    }
    if (ev.ev === "tool") {
      push({ kind: "tool", t: ev.t, tool: ev.tool, arguments: ev.arguments, latency: ev.latency_s ?? 0 });
      return;
    }
    if (ev.ev === "phases") {
      push({ kind: "phases", t: ev.t, phases: ev.phases ?? [] });
      return;
    }
    if (ev.ev === "note") {
      push({ kind: "note", t: ev.t, text: ev.text ?? "" });
      return;
    }
    if (ev.ev === "step") {
      run.phase = ev.phase ?? run.phase;
      push({
        kind: "step",
        t: ev.t,
        step: ev.step,
        phase: ev.phase ?? 0,
        decision: ev.decision ?? {},
        observation: ev.observation ?? {},
        images: ev.images ?? null,
        latency: ev.latency_s ?? 0,
        batch: ev.batch ?? null,
        execution: null,
      });
      return;
    }
    if (ev.ev === "execution") {
      // Land on the step this outcome belongs to; a recovery with no decision
      // of its own becomes its own row rather than being dropped.
      const target = [...run.feed].reverse().find((e) => e.kind === "step" && e.step === ev.step && !e.execution);
      if (target) target.execution = ev.execution ?? {};
      else push({ kind: "execution", t: ev.t, step: ev.step, execution: ev.execution ?? {} });
    }
  }

  const unsub = ros.subscribe(
    ICL_TRACE_TOPIC,
    (msg) => {
      const raw = msg?.data ?? msg?.msg?.data;
      if (typeof raw !== "string") return;
      try {
        apply(JSON.parse(raw));
      } catch (err) {
        console.warn("[icl] unparseable trace event:", err);
        return;
      }
      onChange(run);
    },
    undefined,
    "std_msgs/msg/String",
  );

  return { run: () => run, destroy: unsub };
}
