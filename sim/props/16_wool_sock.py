"""Heavier, firmer wool variant; reuses the cotton pattern, not its state."""

from pathlib import Path

import numpy as np
from mars_sim_driver.softbody import SoftProp

_CLOTH_DATA = Path(__file__).resolve().parents[1] / "assets/softbodies/soft_sock/cloth_data.npz"
if not _CLOTH_DATA.is_file():
    raise FileNotFoundError("wool_sock requires the installed soft_sock asset bundle")
with np.load(_CLOTH_DATA) as _stored:
    _CONTROL_COUNT = len(_stored["vertices"])
    _RENDER_COUNT = int(_stored["render_vertex_count"])

# Deliberate simulator preset, not a measured constitutive model of wool.
# Defaults of the existing cotton sidecar are intentionally left unchanged.
PROP = SoftProp(
    name="wool_sock",
    label="🧦",
    title="Wool sock",
    data="../assets/softbodies/soft_sock/cloth_data.npz",
    texture="../assets/softbodies/soft_sock/texture_base_color.png",
    deformable_id=2,
    rgba=(0.65, 0.55, 0.43, 1.0),
    size=(0.066, 0.004, 0.10),
    mass=0.060,
    max_timestep=0.0005,
    condim=3,
    contact_priority=3,
    friction=(0.8, 0.005, 0.0001),
    radius=0.0015,
    contact_margin=0.001,
    contact_solimp=(0.9999, 0.99999, 0.0001),
    bend_stiffness=1.0e-5,
    bend_damping_ratio=0.04,
    xpbd_bending_model=1,
    xpbd_bend_stiffness=0.1,
    xpbd_panel_bend_stiffness=50.0,
    xpbd_contact_thickness=0.0025,
    rest_z=0.002,
    drop_z=0.35,
    reach=(0.6, 0.0),
    viewer={
        "glb": "/models/soft_sock.glb",
        "rotateToZUp": False,
        "clothMaterial": "wool",
        "deformable": {
            "id": 2,
            "controlVertexCount": _CONTROL_COUNT,
            "renderVertexCount": _RENDER_COUNT,
            "skin": "/models/soft_sock_skin.bin",
            "space": "world",
        },
    },
)
