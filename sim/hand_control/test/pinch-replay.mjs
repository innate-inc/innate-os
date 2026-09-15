// node test/pinch-replay.mjs BASE_PROFILE CANDIDATE_PROFILE REFINE_STUDY [REPORT]
// Replays every held-pose recording. Separate clips are not treated as an
// observed movement trajectory. Every *_repeat stays outside calibration.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { PinchMapper, pinchSample } from "../src/pinch.mjs";
import { PersonalMapper, personalSample } from "../src/personal.mjs";
const [
  baseFile,
  candidateFile,
  refineDirectory,
  output = "artifacts/pinch-mapping/replay.json",
] = process.argv.slice(2);
if (!refineDirectory)
  throw new Error(
    "Pass baseline profile, candidate profile, and refinement study.",
  );
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const baseline = JSON.parse(fs.readFileSync(baseFile)),
  candidate = JSON.parse(fs.readFileSync(candidateFile));
const median = (a) => a.toSorted((a, b) => a - b)[a.length >> 1],
  vectorMedian = (a) => a[0].map((_, i) => median(a.map((v) => v[i])));
const rows = [];
for (const [group, directory] of [
  ["original", path.join(root, "study-data", baseline.session)],
  ["floor", path.join(root, "study-data/floor", baseline.floor.session)],
  ["refine", refineDirectory],
]) {
  const manifest = JSON.parse(
    fs.readFileSync(path.join(directory, "manifest.json")),
  );
  for (const pose of manifest.poses) {
    const rec = JSON.parse(
      fs.readFileSync(path.join(directory, manifest.matches[pose.id].record)),
    );
    const result = (f) => ({
      landmarks: [f.landmarks],
      worldLandmarks: [f.world_landmarks],
    });
    rows.push({
      group,
      id: pose.id,
      role: pose.role,
      target: [
        pose.angles[4],
        pose.angles.slice(1, 4).reduce((a, b) => a + b),
        pose.angles[0],
      ],
      baseline: rec.frames.map((f) =>
        personalSample(result(f), rec.camera.width, rec.camera.height),
      ),
      candidate: rec.frames.map((f) =>
        pinchSample(result(f), rec.camera.width, rec.camera.height),
      ),
    });
  }
}
const errors = { baseline: [], candidate: [] },
  poses = [];
for (const row of rows) {
  const ref = rows.find((r) => r.group === row.group),
    pose = { id: row.id, role: row.role };
  for (const kind of ["baseline", "candidate"]) {
    const mapper =
        kind === "baseline"
          ? new PersonalMapper(baseline)
          : new PinchMapper(candidate),
      start = ref[kind].filter((s) => s.valid);
    mapper.anchor(start[start.length >> 1], [0, 0, 0], 0.55, [0, 0, 0]);
    if (kind === "candidate") mapper.resetObservation();
    let t = 0;
    const samples = row[kind].filter((s) => s.valid);
    for (let i = 0; i < 30; i++) mapper.map(samples[0], (t += 33));
    const outputs = samples
      .map((s) => mapper.map(s, (t += 33)))
      .filter(Boolean);
    const wrist = vectorMedian(outputs.map((o) => o.wrist));
    const target = ["left", "right", "high", "low", "forward", "back"].includes(
      row.id,
    )
      ? [0, 0, 0]
      : row.target.map((v, i) => v - ref.target[i]);
    const error = wrist.map((v, i) => ((v - target[i]) * 180) / Math.PI);
    pose[kind] = {
      wristDegrees: wrist.map((v) => (v * 180) / Math.PI),
      errorDegrees: error,
      grip: median(outputs.map((o) => o.grip)),
      position: vectorMedian(outputs.map((o) => o.position)),
    };
    if (row.role === "repeat_check") errors[kind].push(...error);
  }
  poses.push(pose);
}
const rms = Object.fromEntries(
  Object.entries(errors).map(([k, e]) => [
    k,
    Math.sqrt(e.reduce((s, v) => s + v * v, 0) / e.length),
  ]),
);
const report = {
  baselineRevision: baseline.revision,
  candidateRevision: candidate.revision,
  repeatCount: errors.baseline.length / 3,
  repeatAxisRmsDegrees: rms,
  scope:
    "Recorded held poses; four reserved repeats, excluded from fitting and hyperparameter selection. This does not measure subjective intuitiveness.",
  poses,
};
fs.mkdirSync(path.dirname(output), { recursive: true });
fs.writeFileSync(output, JSON.stringify(report, null, 2) + "\n");
console.log(
  JSON.stringify(
    {
      repeatCount: report.repeatCount,
      repeatAxisRmsDegrees: rms,
      report: output,
    },
    null,
    2,
  ),
);
