import { mkdir, readFile, writeFile, cp } from "node:fs/promises";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";

const root = new URL("./", import.meta.url);
const cache = new URL(".cache/", root);
await mkdir(new URL("wasm/", cache), { recursive: true });
await cp(new URL("favicon.svg", root), new URL("favicon.svg", cache));
await cp(
  new URL("node_modules/@mediapipe/tasks-vision/wasm/", root),
  new URL("wasm/", cache),
  { recursive: true },
);
const expected =
  "fbc2a30080c3c557093b5ddfc334698132eb341044ccee322ccf8bcf3607cde1";
let model;
for (const path of [
  new URL("hand_landmarker.task", cache),
  new URL(
    "../../webapp/public/vendor/mediapipe-0.10.32/hand_landmarker.task",
    root,
  ),
]) {
  try {
    model = await readFile(path);
    break;
  } catch {
    /* fetch if no local model */
  }
}
if (!model) {
  const response = await fetch(
    "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task",
  );
  if (!response.ok)
    throw new Error(`Model download failed: ${response.status}`);
  model = Buffer.from(await response.arrayBuffer());
}
if (createHash("sha256").update(model).digest("hex") !== expected)
  throw new Error("Hand model integrity check failed");
await writeFile(new URL("hand_landmarker.task", cache), model);
console.log(`Verified local model and WASM assets in ${fileURLToPath(cache)}`);
