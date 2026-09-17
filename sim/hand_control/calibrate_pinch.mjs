// node calibrate_pinch.mjs BASE_PROFILE REFINE_STUDY [CANDIDATE_PROFILE]
// Recordings remain local. This produces a candidate; it never replaces the
// live calibration. Repeat poses are reserved for evaluation by the fitter.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { pinchSample } from "./src/pinch.mjs";

const root = path.dirname(fileURLToPath(import.meta.url));
const [
  baseFile,
  refineDirectory,
  output = "artifacts/pinch-mapping/candidate-profile.json",
] = process.argv.slice(2);
if (!baseFile || !refineDirectory)
  throw new Error("Pass a base profile and completed refinement study.");
const profile = JSON.parse(fs.readFileSync(baseFile));
const directories = [
  ["original", path.join(root, "study-data", profile.session)],
  ["floor", path.join(root, "study-data/floor", profile.floor.session)],
  ["refine", path.resolve(refineDirectory)],
];
const hash = createHash("sha256"),
  rows = [];
let session;
for (const [group, directory] of directories) {
  const manifest = JSON.parse(
    fs.readFileSync(path.join(directory, "manifest.json")),
  );
  if (group === "refine") session = manifest.session;
  for (const pose of manifest.poses) {
    const entry = manifest.matches[pose.id];
    if (entry?.comfort !== "comfortable")
      throw new Error(`Review or record ${pose.id} before fitting.`);
    const bytes = fs.readFileSync(path.join(directory, entry.record));
    hash.update(bytes);
    const recording = JSON.parse(bytes);
    const samples = recording.frames
      .map((f) =>
        pinchSample(
          { landmarks: [f.landmarks], worldLandmarks: [f.world_landmarks] },
          recording.camera.width,
          recording.camera.height,
        ),
      )
      .filter((s) => s.valid);
    if (samples.length < 8)
      throw new Error(`Not enough valid frames for ${pose.id}.`);
    rows.push({
      group,
      id: pose.id,
      grip: pose.grip,
      role: pose.role,
      target: [
        pose.angles[4],
        pose.angles.slice(1, 4).reduce((a, b) => a + b, 0),
        pose.angles[0],
      ],
      pinch: samples.map((s) => ({
        features: s.features,
        stableFeatures: s.stableFeatures,
        visible: s.visible,
        valid: true,
      })),
    });
  }
}
fs.mkdirSync(path.dirname(output), { recursive: true });
const dataFile = output + ".features.json";
fs.writeFileSync(
  dataFile,
  JSON.stringify({ rows, session, source_hash: hash.digest("hex") }),
);
const python =
  process.env.STUDIO_PYTHON || path.join(root, "../.venv/bin/python");
const result = spawnSync(
  python,
  [
    path.join(root, "calibrate_pinch.py"),
    dataFile,
    path.resolve(baseFile),
    path.resolve(output),
  ],
  { stdio: "inherit" },
);
if (result.error) throw result.error;
if (result.status !== 0) process.exit(result.status || 1);
