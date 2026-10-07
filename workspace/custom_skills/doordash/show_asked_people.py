# SPDX-License-Identifier: Apache-2.0
import json
from pathlib import Path

from innate import Skill, SkillOutput

FACES_DIR = Path.home() / "lunch_faces"
ORDERS = Path.home() / "doordash_orders.json"
TILE = 220  # px per face tile on the contact sheet


class ShowAskedPeople(Skill):
    """Show the faces and names of everyone whose lunch order you already
    recorded. Call this whenever you see a person and are not sure whether
    you already asked them — compare their face to the attached photo sheet
    before greeting them."""

    def execute(self) -> SkillOutput:
        orders = json.loads(ORDERS.read_text()) if ORDERS.exists() else []
        names = [o["name"] for o in orders]
        if not names:
            return SkillOutput("Nobody has been asked yet — everyone you meet is new.")

        summary = "; ".join(f"{o['name']}: {o['order']}" for o in orders)
        sheet = self._contact_sheet(names)
        if sheet is None:
            return SkillOutput(
                f"Asked so far ({len(names)}): {summary}. (No face photos saved yet — "
                "use remember_face after each order.)"
            )
        return SkillOutput(
            f"Asked so far ({len(names)}): {summary}. The attached sheet shows their "
            "faces with names — compare before asking anyone.",
            image=sheet,
        )

    def _contact_sheet(self, names: list[str]) -> bytes | None:
        try:
            import cv2
            import numpy as np
        except ImportError:
            return None
        tiles = []
        for name in names:
            safe = "".join(c for c in name.strip().lower() if c.isalnum() or c in "-_")
            path = FACES_DIR / f"{safe}.jpg"
            if not path.exists():
                continue
            img = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                continue
            h, w = img.shape[:2]
            side = min(h, w)
            img = img[(h - side) // 2 : (h + side) // 2, (w - side) // 2 : (w + side) // 2]
            img = cv2.resize(img, (TILE, TILE))
            label = np.full((36, TILE, 3), 255, np.uint8)
            cv2.putText(label, name[:18], (6, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 2)
            tiles.append(np.vstack([img, label]))
        if not tiles:
            return None
        ok, jpeg = cv2.imencode(".jpg", np.hstack(tiles), [cv2.IMWRITE_JPEG_QUALITY, 85])
        return jpeg.tobytes() if ok else None
