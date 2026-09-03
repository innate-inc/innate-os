// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc

import assert from "node:assert/strict";
import { PcmAudioPlayer } from "../js/pcmAudioPlayer.js";

const starts = [];
const stops = [];
let decoded = null;
const context = {
  currentTime: 10,
  destination: {},
  createBuffer(_channels, length, sampleRate) {
    const channel = new Float32Array(length);
    decoded = channel;
    return { duration: length / sampleRate, getChannelData: () => channel, channel };
  },
  createBufferSource() {
    return {
      connect() {},
      start: (at) => starts.push(at),
      stop: () => stops.push(true),
      onended: null,
      buffer: null,
    };
  },
};

const pcm = Buffer.from(new Int16Array([-32768, 0, 32767]).buffer).toString("base64");
const player = new PcmAudioPlayer(/** @type {any} */ (context));
assert.equal(player.push({ sample_rate: 48_000, pcm }), true);
assert.deepEqual(starts, [10.12]);
assert.deepEqual([...decoded], [-1, 0, 32767 / 32768]);
assert.equal(player.nextAt, 10.12 + 3 / 48_000);
assert.equal(player.push({ sample_rate: 0, pcm }), false);
assert.equal(player.push({ sample_rate: 48_000, pcm: "not base64!" }), false);
player.nextAt = context.currentTime + 1;
assert.equal(player.push({ sample_rate: 48_000, pcm }), false);
assert.deepEqual(starts, [10.12]);
player.nextAt = 0;
assert.equal(player.push({ sample_rate: 48_000, pcm }), true);
player.reset();
assert.equal(stops.length, 2);
assert.equal(player.nextAt, 0);

console.log("ok - simulator PCM is decoded, bounded, and stopped when playback resets");
