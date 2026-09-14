// Usage: node calibrate.mjs study-data/SESSION [output-profile.json]
// Fit pose medians with equal weight. The final neutral take is never fitted.
import fs from "node:fs";
import path from "node:path";
import { createHash } from "node:crypto";
import {
  personalSample,
  thumbBaseFrame,
  rotationVector,
  multiply,
  anglesFor,
  shapeFeatures,
  correctedCamera,
  gripFor,
} from "./src/personal.mjs";
import { measureHand } from "./src/control.mjs";
import { relativeAngles } from "./src/orientation.mjs";

import { median, vectorMedian, ridge } from "./calibration_math.mjs";
const subtract = (a, b) => a.map((v, i) => v - b[i]);

const directory = process.argv[2];
if (!directory) throw new Error("Pass the completed pose-study directory.");
const manifest = JSON.parse(
  fs.readFileSync(path.join(directory, "manifest.json")),
);
const records = Object.fromEntries(
  Object.entries(manifest.matches).map(([id, m]) => [
    id,
    JSON.parse(fs.readFileSync(path.join(directory, m.record))),
  ]),
);
const required = [
  "neutral",
  "pitch_up",
  "pitch_down",
  "roll_left",
  "roll_right",
  "yaw_left",
  "yaw_right",
  "left",
  "right",
  "high",
  "low",
  "forward",
  "back",
  "closed",
  "open",
  "neutral_repeat",
];
for (const id of required)
  if (records[id]?.comfort !== "comfortable")
    throw new Error(`Review or record the ${id} match before fitting.`);
const samples = Object.fromEntries(
  required.map((id) => [
    id,
    records[id].frames
      .map((f) =>
        personalSample(
          {
            landmarks: [f.landmarks],
            worldLandmarks: [f.world_landmarks],
            handedness: [f.handedness],
          },
          records[id].camera.width,
          records[id].camera.height,
        ),
      )
      .filter((s) => s.valid),
  ]),
);
for (const id of required)
  if (samples[id].length < 8)
    throw new Error(`Insufficient valid frames: ${id}`);
const points = records.neutral.frames[0].world_landmarks.map((_, i) =>
  Object.fromEntries(
    ["x", "y", "z"].map((axis) => [
      axis,
      median(records.neutral.frames.map((f) => f.world_landmarks[i][axis])),
    ]),
  ),
);
const reference = thumbBaseFrame(points);
const training = required.filter((id) => id !== "neutral_repeat");
const vectors = Object.fromEntries(
  required.map((id) => [
    id,
    vectorMedian(
      samples[id].map((s) => rotationVector(s.orientation, reference)),
    ),
  ]),
);
const targets = Object.fromEntries(
  required.map((id) => [
    id,
    [
      records[id].target.angles[4],
      records[id].target.angles.slice(1, 4).reduce((a, b) => a + b, 0),
    ],
  ]),
);
const matrix = ridge(
  training.map((id) => vectors[id]),
  training.map((id) => targets[id]),
  0.01,
);
const profile = {
  version: 1,
  session: manifest.session,
  created_at: new Date().toISOString(),
  label: "Your taught gestures",
  source_hash: createHash("sha256")
    .update(JSON.stringify(manifest.matches))
    .digest("hex"),
  reference,
  rotation: { matrix, response: [] },
  grip: {
    knots: ["closed", "neutral", "open"].map((id) =>
      median(samples[id].map((s) => s.aperture)),
    ),
    neutral: records.neutral.target.grip,
  },
  workspace: {
    center: records.neutral.target.ee,
    span: [0.045, 0.1, 0.13],
    angles: records.neutral.target.angles,
    grip: records.neutral.target.grip,
  },
  view: records.neutral_repeat.camera.robot_view.start,
};
if (
  profile.grip.knots[1] - profile.grip.knots[0] < 0.1 ||
  profile.grip.knots[2] - profile.grip.knots[1] < 0.1
)
  throw new Error("Open/neutral/closed finger gaps overlap too much.");
for (const [i, negative, positive] of [
  [0, "roll_left", "roll_right"],
  [1, "pitch_up", "pitch_down"],
]) {
  const noise = samples.neutral.map(
    (s) => multiply(matrix, rotationVector(s.orientation, reference))[i],
  );
  const deadzone = Math.min(
    0.035,
    Math.max(0.012, 3 * median(noise.map((v) => Math.abs(v - median(noise))))),
  );
  const low = multiply(matrix, vectors[negative])[i],
    high = multiply(matrix, vectors[positive])[i];
  if (low >= -deadzone || high <= deadzone)
    throw new Error("Rotation directions are ambiguous.");
  const response = {
    deadzone,
    negative: Math.abs(targets[negative][i]) / (Math.abs(low) - deadzone),
    positive: targets[positive][i] / (high - deadzone),
  };
  if (Math.max(response.negative, response.positive) > 3)
    throw new Error("Rotation needs more distinct examples.");
  profile.rotation.response.push(response);
}
const angles = Object.fromEntries(
  required.map((id) => [
    id,
    vectorMedian(samples[id].map((s) => anglesFor(s, profile))),
  ]),
);
const camera = Object.fromEntries(
  required.map((id) => [id, vectorMedian(samples[id].map((s) => s.camera))]),
);
const shape = Object.fromEntries(
  required.map((id) => [
    id,
    vectorMedian(
      samples[id].map((s) =>
        shapeFeatures(anglesFor(s, profile), s.aperture, profile.grip.knots[1]),
      ),
    ),
  ]),
);
// Rotation changes the visible palm center/size even for an intended pure tilt.
// Learn that displacement from the four rotation examples; the open/closed
// difference isolates finger closure without learning the session's rest drift.
const rotations = ["pitch_up", "pitch_down", "roll_left", "roll_right"];
// The four rotation takes all request the same jaw opening. Correct their
// systematic aperture change before applying the thumb/index gap calibration.
profile.grip.rotationCompensation = ridge(
  rotations.map((id) => shape[id].slice(0, 4)),
  rotations.map((id) => [
    median(samples[id].map((s) => s.aperture)) - profile.grip.knots[1],
  ]),
  0.001,
)[0];
profile.grip.knots = ["closed", "neutral", "open"].map((id) =>
  median(
    samples[id].map(
      (s) =>
        s.aperture -
        multiply(
          [profile.grip.rotationCompensation],
          shapeFeatures(anglesFor(s, profile), 0, 0).slice(0, 4),
        )[0],
    ),
  ),
);
const nuisanceX = rotations.map((id) => subtract(shape[id], shape.neutral));
const nuisanceY = rotations.map((id) => subtract(camera[id], camera.neutral));
nuisanceX.push(subtract(shape.open, shape.closed));
nuisanceY.push(subtract(camera.open, camera.closed));
profile.position = { compensation: ridge(nuisanceX, nuisanceY, 0.001) };
const clean = Object.fromEntries(
  required.map((id) => [
    id,
    vectorMedian(samples[id].map((s) => correctedCamera(s, profile))),
  ]),
);
const pairs = [
  ["left", "right"],
  ["high", "low"],
  ["forward", "back"],
];
profile.position.matrix = ridge(
  pairs.map(([a, b]) => subtract(clean[a], clean[b])),
  pairs.map(([a, b]) => subtract(records[a].target.ee, records[b].target.ee)),
  0.0005,
);
profile.position.reference = clean.neutral;
const oldReference = measureHand({
  landmarks: [records.neutral.frames[0].landmarks],
  worldLandmarks: [records.neutral.frames[0].world_landmarks],
}).orientation;
const rows = required.map((id) => {
  const s = samples[id],
    r = records[id];
  const predicted = s.map((v) => anglesFor(v, profile)),
    med = vectorMedian(predicted);
  const xyz = multiply(
    profile.position.matrix,
    subtract(clean[id], clean.neutral),
  );
  const old = vectorMedian(
    r.frames.map((f) =>
      relativeAngles(
        measureHand({
          landmarks: [f.landmarks],
          worldLandmarks: [f.world_landmarks],
        }).orientation,
        oldReference,
      ),
    ),
  ).slice(0, 2);
  return {
    pose: id,
    frames: s.length,
    target_degrees: targets[id].map((v) => (v * 180) / Math.PI),
    predicted_degrees: med.map((v) => (v * 180) / Math.PI),
    old_degrees: old.map((v) => (v * 180) / Math.PI),
    within_take_std_degrees: [0, 1].map(
      (i) =>
        (Math.sqrt(
          predicted.reduce((n, v) => n + (v[i] - med[i]) ** 2, 0) / s.length,
        ) *
          180) /
        Math.PI,
    ),
    position_delta_mm: xyz.map((v) => v * 1000),
    grip: median(
      s.map((v) => gripFor(v.aperture, profile, anglesFor(v, profile))),
    ),
  };
});
const report = {
  session: manifest.session,
  frames: rows.reduce((n, r) => n + r.frames, 0),
  training_poses: training,
  consistency_check:
    "neutral_repeat excluded from fitting; this is a single-session calibration, not a general accuracy benchmark",
  rows,
};
const destination = process.argv[3] || "study-data/active-profile.json";
profile.revision = createHash("sha256")
  .update(JSON.stringify(profile))
  .digest("hex");
fs.mkdirSync(path.dirname(destination), { recursive: true });
if (fs.existsSync(destination))
  fs.copyFileSync(destination, `${destination}.${Date.now()}.previous`);
fs.writeFileSync(destination, JSON.stringify(profile, null, 2));
fs.writeFileSync(
  path.join(directory, "calibration-report.json"),
  JSON.stringify(report, null, 2),
);
console.log(
  JSON.stringify(
    {
      profile: destination,
      frames: report.frames,
      rotation: profile.rotation,
      position: profile.position,
      grip: profile.grip,
      rows: rows.map((r) => ({
        pose: r.pose,
        angles: r.predicted_degrees.map((v) => +v.toFixed(1)),
        xyz: r.position_delta_mm.map((v) => Math.round(v)),
        grip: +r.grip.toFixed(2),
      })),
    },
    null,
    2,
  ),
);
