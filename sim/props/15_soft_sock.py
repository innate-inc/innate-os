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

# The asset/stream describes the sewn cloth surface. The browser adds visual
# subdivision; native cameras and collision retain the coarse surface.
PROP = SoftProp(
    name="soft_sock",
    label="🧦",
    title="Soft sock",
    data="../assets/softbodies/soft_sock/cloth_data.npz",
    texture="../assets/softbodies/soft_sock/texture_base_color.png",
    deformable_id=1,
    rgba=(0.34117647, 0.57254902, 0.72156863, 1.0),
    # The generated local frame is XY-centred with its lowest point at z=0.
    size=(0.066, 0.002, 0.10),
    mass=0.025,
    # Thin fabric needs smaller contact steps while it is present. The world
    # keeps its normal timestep when the sock is parked or removed.
    max_timestep=0.0005,
    condim=3,
    contact_priority=3,
    friction=(0.8, 0.005, 0.0001),
    # Model the folded fabric as a thin shell instead of an effectively
    # zero-thickness mathematical surface.  The contact margin lets MuJoCo
    # establish the constraint before a narrow fingertip crosses the shell.
    radius=0.001,
    contact_margin=0.001,
    # Cloth vertices are far lighter than the rigid props. Soft, mass-scaled
    # contact compliance lets a loaded fingertip cross a sub-mm sheet.
    contact_solimp=(0.9999, 0.99999, 0.0001),
    # Bending and in-plane extension are separate: retain edge constraints
    # against rubber-band stretch, but let the thin panels drape and swing.
    # Contact priority 3 deliberately avoids the rigid fingers' condim=6
    # torsional/rolling resistance, which is inappropriate for cloth.
    bend_stiffness=1.0e-7,
    bend_damping_ratio=0.04,
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
