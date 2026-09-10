# First life

The successful Backrooms ending (`way_out`) is a live Three.js diorama. MARS uses
its actual shipped URDF and STL models, with a raised arm and upward gaze. The
boot, soil, laces, and curved leaves are procedural geometry in
`sim/viewer/src/plantRewardScene.ts`. The camera sweeps around them for the reveal;
mouse/touch drag and arrow keys let the player orbit afterwards. Geometry,
materials, textures, shadows, controls, and the WebGL context are released on close,
and pending robot-model fetches are aborted.

`first-life.png` is used only as the small inventory thumbnail. The original
transparent PNG is used without image edits.
Generated with the built-in imagegen tool on 2026-09-10; the user supplied a
plant-in-a-boot reference and requested a WALL-E-inspired reward with the
satisfying pacing of a Super Mario Galaxy star celebration. The musical phrase
is synthesized originally in `plantReward.js`; no game audio is sampled.

Final generation prompt:

> Use case: stylized-concept. Asset type: transparent cutout game victory collectible for a robot adventure. Create one beautiful, highly detailed cinematic 3D render of a small, old brown leather lace-up work boot with a living green seedling growing out of dark soil inside its open ankle. Evoke the touching plant-in-a-boot discovery from WALL-E. Boot toe points left, three-quarter side view, whole object visible including all leaves, centered in a square with 12% clear padding. Squat worn boot, chunky dark sole, scuffed leather, brass eyelets, loosely crossed laces. Graceful slim curved stem with four fresh translucent green leaves above it. Rich tactile materials, charming animated film quality, warm golden key light upper left and delicate mint green rim light right, bright readable leather in shadows. This will float against a dark teal victory screen. Genuinely TRANSPARENT background with alpha. No ground, no background, no glow baked outside silhouette, no cast shadow, no particles, no lettering, no frame, no other objects. Save the generated asset to a local file and return its path.

Preview the actual production component without ROS or the robot:

```sh
npm run build:lib --prefix sim/viewer
python3 sim/tools/preview_reward.py
# Open http://localhost:8790/debug/plant-reward.html
```

The preview includes WebGL cleanup checks, lifecycle checks, and real `createAgentStudio` subscription tests
with a fake world and brain. Check normal and reduced motion, phone and desktop,
Escape, mute, collection, and replay. A browser-local keepsake lasts across
scene changes and page visits; session storage prevents replaying an already
collected attempt. This is a presentation reward, not a physics prop or grasp.
