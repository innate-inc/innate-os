// node calibrate_floor.mjs BASE_PROFILE FLOOR_STUDY [CANDIDATE_PROFILE]
// Produces a candidate for replay; the live profile is not changed by default.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createHash } from "node:crypto";
import { median, vectorMedian, ridge } from "./calibration_math.mjs";
import {
  personalSample,
  baseAnglesFor,
  floorFeatures,
  anglesFor,
  positionFor,
  personalGrip,
} from "./src/personal.mjs";

const root = path.dirname(fileURLToPath(import.meta.url));
const [
  baseFile,
  floorDirectory,
  output = "artifacts/floor-mapping/candidate-profile.json",
] = process.argv.slice(2);
if (!baseFile || !floorDirectory)
  throw new Error("Pass a base profile and completed floor study.");
const profile = JSON.parse(fs.readFileSync(baseFile));
const baseRevision = profile.floor?.base_revision || profile.revision;
delete profile.floor;
const hash = (value) =>
  createHash("sha256").update(JSON.stringify(value)).digest("hex");
function load(directory, group) {
  const manifest = JSON.parse(
    fs.readFileSync(path.join(directory, "manifest.json")),
  );
  const rows = manifest.poses.map((pose) => {
    const entry = manifest.matches[pose.id];
    if (entry?.comfort !== "comfortable")
      throw new Error(
        "Review or record the " + pose.id + " match before fitting.",
      );
    const record = JSON.parse(
      fs.readFileSync(path.join(directory, entry.record)),
    );
    const samples = record.frames
      .map((frame) =>
        personalSample(
          {
            landmarks: [frame.landmarks],
            worldLandmarks: [frame.world_landmarks],
            handedness: [frame.handedness],
          },
          record.camera.width,
          record.camera.height,
        ),
      )
      .filter((s) => s.valid);
    if (samples.length < 8)
      throw new Error("Not enough valid frames for " + pose.id);
    return {
      id: pose.id,
      group,
      samples,
      pose,
      target: [
        pose.angles[4],
        pose.angles.slice(1, 4).reduce((a, b) => a + b, 0),
      ],
    };
  });
  return { manifest, rows };
}
const original = load(
  path.join(root, "study-data", profile.session),
  "original",
);
const floor = load(floorDirectory, "floor");
const expected = [
  "floor_reference",
  "floor_down_mid",
  "floor_down_steep",
  "floor_open",
  "floor_closed",
  "floor_lift",
  "floor_repeat",
];
if (expected.join() !== floor.rows.map((r) => r.id).join())
  throw new Error("Expected the seven-pose floor exercise.");
const rows = [...original.rows, ...floor.rows];
const training = rows.filter((row) => row.pose.role !== "repeat_check");
const reference = floor.rows[0];
const baseReference = vectorMedian(
  reference.samples.map((s) => positionFor(s, profile)),
);
const scales = [1, 1, 1, 1, 1, 0.3, 0.3, 0.2];
const width = 0.65;
const centers = training.map((row) =>
  vectorMedian(row.samples.map((s) => floorFeatures(s, profile))),
);
const basis = (sample) => {
  const features = floorFeatures(sample, profile);
  return centers.map((center) =>
    Math.exp(
      -features.reduce(
        (sum, v, i) => sum + ((v - center[i]) * scales[i]) ** 2,
        0,
      ) /
        (2 * width ** 2),
    ),
  );
};
const x = [],
  y = [],
  weights = [];
for (const row of training) {
  for (const sample of row.samples) {
    x.push(basis(sample));
    weights.push(1 / row.samples.length); // Equal weight per demonstrated pose.
    const angles = baseAnglesFor(sample, profile);
    const point = positionFor(sample, profile);
    y.push(
      row.group === "original"
        ? [0, 0, 0, 0, 0]
        : [
            ...row.target.map((v, i) => v - angles[i]),
            ...row.pose.ee.map(
              (v, i) =>
                v - reference.pose.ee[i] - (point[i] - baseReference[i]),
            ),
          ],
    );
  }
}
const fit = ridge(x, y, 0.003, weights);
const byId = Object.fromEntries(rows.map((r) => [r.id, r]));
const gaps = (id) =>
  byId[id].samples.map((s) => s.screenAperture).toSorted((a, b) => a - b);
const gap = (id) => median(gaps(id));
const closed = Math.max(
  ...["floor_closed", "floor_lift"].map((id) => {
    const values = gaps(id);
    return values[Math.floor(0.9 * (values.length - 1))];
  }),
);
const mid = Math.PI / 4,
  deep = (80 * Math.PI) / 180;
profile.floor = {
  version: 1,
  session: floor.manifest.session,
  source_hash: hash(floor.manifest.matches),
  base_revision: baseRevision,
  scales,
  width,
  centers,
  coefficients: centers.map((_, i) => fit.map((column) => column[i])),
  grip: {
    pitch: [0, mid, deep],
    closed: [gap("closed"), closed, closed],
    neutral: [gap("neutral"), gap("floor_down_mid"), gap("floor_down_steep")],
    open: [
      gap("open"),
      gap("open") + ((gap("floor_open") - gap("open")) * mid) / deep,
      gap("floor_open"),
    ],
  },
};
const newReference = vectorMedian(
  reference.samples.map((s) => positionFor(s, profile)),
);
const report = {
  session: floor.manifest.session,
  frames: floor.rows.reduce((n, row) => n + row.samples.length, 0),
  training: training.map((row) => row.id),
  held_out: rows
    .filter((row) => row.pose.role === "repeat_check")
    .map((row) => row.id),
  interpretation:
    "Calibration replay, not an independent accuracy benchmark. Both neutral repeats are excluded. Translation uses a live relative reference because resting hand position and distance drift across takes.",
  rows: rows.map((row) => {
    const angles = row.samples.map((s) => anglesFor(s, profile));
    const base = row.samples.map((s) => baseAnglesFor(s, profile));
    return {
      pose: row.id,
      frames: row.samples.length,
      target_degrees: row.target.map((v) => (v * 180) / Math.PI),
      degrees: vectorMedian(angles).map((v) => (v * 180) / Math.PI),
      correction_degrees: vectorMedian(
        angles.map((r, j) => r.map((v, i) => v - base[j][i])),
      ).map((v) => (v * 180) / Math.PI),
      std_degrees: [0, 1].map((i) => {
        const mean = angles.reduce((n, a) => n + a[i], 0) / angles.length;
        return (
          (Math.sqrt(
            angles.reduce((n, a) => n + (a[i] - mean) ** 2, 0) / angles.length,
          ) *
            180) /
          Math.PI
        );
      }),
      position_delta_mm: vectorMedian(
        row.samples.map((s) => positionFor(s, profile)),
      ).map((v, i) => (v - newReference[i]) * 1000),
      grip: median(row.samples.map((s) => personalGrip(s, profile))),
    };
  }),
};
profile.revision = hash({ ...profile, revision: undefined });
fs.mkdirSync(path.dirname(output), { recursive: true });
if (fs.existsSync(output))
  fs.copyFileSync(output, output + "." + Date.now() + ".previous");
fs.writeFileSync(output, JSON.stringify(profile, null, 2));
fs.writeFileSync(
  path.join(path.dirname(output), "floor-report.json"),
  JSON.stringify(report, null, 2),
);
console.log(JSON.stringify({ profile: output, ...report }, null, 2));
