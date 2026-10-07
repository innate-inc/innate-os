# SPDX-License-Identifier: Apache-2.0
from pathlib import Path

from innate import MainImage, Skill, SkillOutput

FACES_DIR = Path.home() / "lunch_faces"


class RememberFace(Skill):
    """Save a snapshot of the person currently in front of your camera,
    tagged with their name. Call this right after recording someone's order,
    while their face is still in view (use look_at_face first). This is how
    you remember who you already asked."""

    image: MainImage

    def execute(self, person_name: str) -> SkillOutput:
        FACES_DIR.mkdir(exist_ok=True)
        safe = "".join(c for c in person_name.strip().lower() if c.isalnum() or c in "-_") or "unknown"
        path = FACES_DIR / f"{safe}.jpg"
        jpeg = self.image.jpeg
        if not jpeg:
            self.fail("No camera frame available to save.")
        path.write_bytes(jpeg)
        return SkillOutput(
            f"Saved this view as {person_name}'s face. If their face is not clearly "
            "visible in the attached image, adjust your camera with look_at_face and call this again.",
            image=jpeg,
        )
