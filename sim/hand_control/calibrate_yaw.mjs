// node calibrate_yaw.mjs BASE_PROFILE [OUTPUT_PROFILE] [FLOOR_INFERENCE_JSON]
// Reuse the existing pitch/roll/floor recordings; no additional poses needed.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createHash } from "node:crypto";
import { median, vectorMedian, ridge } from "./calibration_math.mjs";
import { personalSample, yawGeometry, yawFor } from "./src/personal.mjs";

const root = path.dirname(fileURLToPath(import.meta.url));
const [
  baseFile,
  output = "artifacts/yaw-mapping/candidate-profile.json",
  inferenceFile,
] = process.argv.slice(2);
if (!baseFile) throw new Error("Pass the existing personal profile.");
const profile = JSON.parse(fs.readFileSync(baseFile));
delete profile.yaw;
const directories = [path.join(root, "study-data", profile.session)];
if (profile.floor)
  directories.push(path.join(root, "study-data/floor", profile.floor.session));
function load(directory, inferred = null) {
  const manifest = JSON.parse(
    fs.readFileSync(path.join(directory, "manifest.json")),
  );
  const rows = Object.entries(manifest.matches).map(([id, entry]) => {
    const record = JSON.parse(
      fs.readFileSync(path.join(directory, entry.record)),
    );
    const samples = (inferred ? inferred[id] : record.frames)
      .map((f) =>
        personalSample(
          { landmarks: [f.landmarks], worldLandmarks: [f.world_landmarks] },
          record.camera.width,
          record.camera.height,
        ),
      )
      .filter((s) => s.valid);
    const geometry = samples.map((s) => {
      const g = yawGeometry(s.orientation, profile.reference);
      return g && { ...g, tilt: [...g.tilt, ...s.yawShape] };
    });
    if (geometry.some((g) => !g) || samples.length < 8)
      throw new Error(`Insufficient observable frames: ${id}`);
    return {
      id,
      source: inferred ? "video-inference" : "recorded",
      samples,
      geometry,
      fit: [
        "neutral",
        "pitch_up",
        "pitch_down",
        "roll_left",
        "roll_right",
        "open",
        "closed",
        "floor_reference",
        "floor_down_mid",
        "floor_down_steep",
        "floor_open",
        "floor_closed",
        "floor_lift",
      ].includes(id),
    };
  });
  const rest = rows.find(
    (p) => p.id === "neutral" || p.id === "floor_reference",
  );
  const baseline = median(rest.geometry.map((g) => g.heading));
  return rows.map((p) => ({ ...p, baseline }));
}
const poses = directories.flatMap((directory) => load(directory));
if (inferenceFile && profile.floor)
  poses.push(
    ...load(directories.at(-1), JSON.parse(fs.readFileSync(inferenceFile))),
  );
const training = poses.filter((p) => p.fit);
// Remove the observed heading accompanying a pure tilt. Inputs are invariant
// to real yaw and contain neither camera position, scale nor fingertip gap.
const split = (p) => Math.floor((p.geometry.length * 2) / 3);
const centers = training.flatMap((p) => {
  const middle = Math.floor(split(p) / 2);
  return [p.geometry.slice(0, middle), p.geometry.slice(middle, split(p))].map(
    (rows) => vectorMedian(rows.map((g) => g.tilt)),
  );
});
const kernel = (tilt, width) =>
  centers.map((c) =>
    Math.exp(
      -tilt.reduce((s, v, i) => s + (v - c[i]) ** 2, 0) / (2 * width ** 2),
    ),
  );
let best;
for (const width of [0.15, 0.2, 0.3, 0.4, 0.55, 0.7]) {
  for (const lambda of [0.0001, 0.001, 0.01, 0.05]) {
    const rows = training.flatMap((p) =>
      p.geometry.slice(0, split(p)).map((g) => ({
        ...g,
        target: g.heading - p.baseline,
        weight: 1 / split(p),
      })),
    );
    const coefficients = ridge(
      rows.map((g) => kernel(g.tilt, width)),
      rows.map((g) => [g.target]),
      lambda,
      rows.map((g) => g.weight),
    )[0];
    if (coefficients.some((c) => Math.abs(c) > 4)) continue;
    const errors = training.map((p) =>
      p.geometry
        .slice(split(p))
        .map(
          (g) =>
            g.heading -
            p.baseline -
            kernel(g.tilt, width).reduce(
              (s, v, i) => s + v * coefficients[i],
              0,
            ),
        ),
    );
    // Equal weight per take, including variation across every accepted frame.
    const loss =
      errors.reduce(
        (s, values) =>
          s + values.reduce((a, v) => a + v * v, 0) / values.length,
        0,
      ) / errors.length;
    if (!best || loss < best.loss) best = { width, coefficients, loss };
  }
}
if (!best) throw new Error("Could not fit a bounded yaw correction.");
profile.yaw = {
  version: 1,
  width: best.width,
  centers,
  coefficients: best.coefficients,
};
profile.revision = createHash("sha256")
  .update(JSON.stringify(profile))
  .digest("hex");
const report = {
  width: best.width,
  rmsDegrees: (Math.sqrt(best.loss) * 180) / Math.PI,
  poses: poses.map((p) => {
    const values = p.samples.map(
      (s) => ((yawFor(s, profile) - p.baseline) * 180) / Math.PI,
    );
    const middle = median(values);
    return {
      id: p.id,
      source: p.source,
      fitted: p.fit,
      medianDegrees: middle,
      p90Deviation: values
        .map((v) => Math.abs(v - middle))
        .sort((a, b) => a - b)[Math.floor(values.length * 0.9)],
    };
  }),
};
fs.mkdirSync(path.dirname(output), { recursive: true });
fs.writeFileSync(output, JSON.stringify(profile, null, 2) + "\n");
fs.writeFileSync(
  path.join(path.dirname(output), "yaw-report.json"),
  JSON.stringify(report, null, 2) + "\n",
);
console.log(JSON.stringify(report, null, 2));
