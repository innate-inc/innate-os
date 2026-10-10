# Gallery

A 9 × 9 m contemporary gallery with eight framed abstract paintings, oak
floorboards, a bench, plants and four ascending display plinths. The room uses
the same authored geometry for the browser and the robot's RGB/depth cameras.

Opening the environment or resetting it restores eight blue cans in a ring
and two red mugs: one on the floor at (-3, 3.2), one on the 0.5 m plinth at
(3, 3.2). Challenge setup starts from this populated room. The floor pickup
location has clear floor and an unobstructed approach; the raised mug is an
observation target, not a shelf-pick request.

The tour starts straight ahead of the robot and proceeds clockwise through
the four wall-aligned cans. Counting explicitly asks the robot to visit the
can ahead before reporting the total. The highest-exhibit task states the
required approach distance and two-second dwell. No compass or invisible
orientation convention is required.

Generate matching textured OBJ/PNG and GLB props with:

```sh
sim/.venv/bin/python sim/tools/build_gallery_props.py
```

After editing the room, regenerate its navigation map:

```sh
PYTHONPATH=ros2_ws/src/mars_bot/mars_sim_driver sim/.venv/bin/python sim/bench/export_nav_map.py --environment gallery --out sim/environments/gallery/map
```

The map exporter parks movable exhibits before scanning; cans and mugs must
not become permanent obstacles in Nav2. `test_gallery_environment.py` checks
default and retry placement, support heights, reachable approaches, and mesh
parity between both renderers. The benchmark oracle checks route and goal
logic, but its fetch action models placement by teleporting the mug; it does
not validate the live agent's perception or physical grasp.
