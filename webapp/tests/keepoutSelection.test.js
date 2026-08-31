import assert from "node:assert/strict";

import { reloadMatchesExpectedMap } from "../js/map/keepoutMask.js";

assert.equal(reloadMatchesExpectedMap("map-b.yaml", "map-b.yaml"), true);
assert.equal(reloadMatchesExpectedMap("map-b.yaml", "map-c.yaml"), false);
assert.equal(reloadMatchesExpectedMap(null, "map-b.yaml"), false);

console.log("3 passed");
