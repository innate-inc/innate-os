"""The authored sock as a real-time deformable cloth prop."""

from pathlib import Path

import numpy as np
from mars_sim_driver.softbody import SoftProp

# Binary simulator assets are installed separately from the git checkout. A
# missing rigid mesh can fall back to a primitive, but a flex has no topology
# without this file; fail during sidecar discovery so props.py skips only this
# optional prop instead of failing the entire world build later.
_CLOTH_DATA = Path(__file__).resolve().parents[1] / "assets/softbodies/soft_sock/cloth_data.npz"
if not _CLOTH_DATA.is_file():
    raise FileNotFoundError("soft_sock cloth data is not installed in the simulator asset bundle")
with np.load(_CLOTH_DATA) as _stored:
    _CONTROL_VERTEX_COUNT = int(np.asarray(_stored["vertices"]).shape[0])
    _RENDER_VERTEX_COUNT = int(np.asarray(_stored["render_vertex_count"]))

# The browser keeps the original 2,496-vertex textured surface and skins it
# from the 42 vertices MuJoCo actually simulates.  Native robot cameras use the
# separately copied texture on the low-resolution flex, so both views retain
# the authored pattern rather than substituting a generic material.
PROP = SoftProp(
    name="soft_sock",
    label="🧦",
    title="Soft sock",
    data="../assets/softbodies/soft_sock/cloth_data.npz",
    texture="../assets/softbodies/soft_sock/texture_base_color.png",
    deformable_id=1,
    rgba=(0.34117647, 0.57254902, 0.72156863, 1.0),
    # The generated local frame is XY-centred with its lowest point at z=0.
    size=(0.0681, 0.0345, 0.1007),
    mass=0.065,
    friction=(0.8, 0.02, 0.002),
    # Ten times the generic cloth default is the stiffest tested setting that
    # still settles within three simulated seconds; higher values kept the
    # coarse cage visibly buzzing after contact.
    bend_stiffness=5.0e-6,
    rest_z=0.001,
    drop_z=0.35,
    # The authored sock is ~20 cm long, so keep it clear of the chassis when
    # placed at the robot; the compact rigid-sock target uses the tighter arm
    # reach separately.
    reach=(0.6, 0.0),
    viewer={
        "glb": "/models/soft_sock.glb",
        "rotateToZUp": False,
        "deformable": {
            "id": 1,
            "controlVertexCount": _CONTROL_VERTEX_COUNT,
            "renderVertexCount": _RENDER_VERTEX_COUNT,
            "skin": "/models/soft_sock_skin.bin",
            "space": "world",
        },
    },
)
