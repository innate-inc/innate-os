# Pantry visuals

The groceries use textured, shaped models in both the robot camera (OBJ +
PNG) and the browser (GLB). Jars have shoulders, glass feet, necks, ribbed
screw lids and paper food labels; cartons and tins have distinct packaging.
The smaller misfiled jam jar uses the same design as the full-size jam jar.
Labels describe contents, never the correct bay or challenge answer.

Regenerate all pantry groceries and the counter jam jar from the repo root:

```sh
sim/.venv/bin/python sim/tools/build_pantry_props.py
```

Generated assets are committed under `objects/` and `viewer/models/groceries/`.
The launcher stages the GLBs into the webapp's public models directory after
installing its asset image, including when using an older image override.
Restart or switch away and back after editing to rebuild the MuJoCo world;
reload the browser after staging updated GLBs.

All geometry and label illustrations are authored by the generator; no
third-party assets or fonts are required. Filled glass is represented with
opaque tinted surfaces so RGB, depth and the browser agree on its silhouette.
The assets retain the existing collider dimensions and body origins. The room
finish layer changes colours and adds non-colliding tile seams and shelf
edges. Shelf boards are 16 mm thick instead of 30 mm, so the 108 mm jars fit
between them; their top surfaces stay at the same heights.

“Count the jars” now starts and retries with five jars on the left unit and
only boxes on the back and right units (two on each). Its answer remains five,
but this grouped layout replaces the original cross-bay search exercise.
The restock, misplaced-item and stocktake tasks now use floor pickup targets
and colour-coded sorting mats that the current floor-grasp skill can attempt.
Shelf stock remains jars on the left and boxes on the other units. Stocktake
explicitly requests the final return to the door. Free play opens with nine
items; each challenge replaces that display with its own exact stock.
Per-scene `Drop(..., z=...)` selects a shelf without changing a prop's defaults.

Validate with native GL on macOS (`MUJOCO_GL=glfw`); on headless Linux use
`MUJOCO_GL=egl` or `osmesa`:

```sh
MUJOCO_GL=glfw sim/.venv/bin/python -m pytest sim/bench/test_pantry_visual_assets.py -q
MUJOCO_GL=glfw sim/.venv/bin/python sim/bench/main.py --map pantry --agents oracle,random --seeds 2 --workers 2
```

Two workers keep the sweep usable alongside the live stack on a 16 GB Mac.
The random agents run to each challenge's time limit, so the full sweep can
take several minutes; `--random-cap 240` explicitly limits that budget.
