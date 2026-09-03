"""Pure OBJ/MTL rewrite used by the licensed town generator.

The marketplace OBJ assigns global Red/Yellow/Green materials to both signal
lenses and decorative objects.  Trimesh groups by material, so preserve each
authored traffic-light object long enough to split its lenses into two phase
groups before loading/exporting the scene.
"""

from __future__ import annotations

SIGNAL_ASPECTS = ("Red", "Yellow", "Green")

SIGNAL_OBJECT_PHASE = {
    "Traffic_Light.001_Cube.009": "NS",
    "Traffic_Light.002_Cube.010": "NS",
    "Traffic_Light.003_Cube.011": "NS",
    "Traffic_Light_Cube.008": "NS",
    "Traffic_Light.009_Cube.018": "NS",
    "Traffic_Light.011_Cube.024": "NS",
    "Traffic_Light.004_Cube.012": "EW",
    "Traffic_Light.005_Cube.013": "EW",
    "Traffic_Light.006_Cube.014": "EW",
    "Traffic_Light.007_Cube.019": "EW",
    "Traffic_Light.008_Cube.016": "EW",
    "Traffic_Light.010_Cube.020": "EW",
}


def _material_blocks(text: str) -> dict[str, list[str]]:
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("newmtl "):
            current = line.removeprefix("newmtl ").strip()
            blocks[current] = []
        elif current is not None:
            blocks[current].append(line)
    return blocks


def isolate_signal_materials(obj_text: str, mtl_text: str) -> tuple[str, str]:
    """Return source text with six signal-only materials and copied MTL data.

    Exactly six heads face each road axis.  Treat that as a source-version
    invariant: silently exporting five live heads would be much harder to spot
    than a build-time error.
    """

    current_phase: str | None = None
    counts = {(phase, aspect): 0 for phase in ("NS", "EW") for aspect in SIGNAL_ASPECTS}
    rewritten: list[str] = []
    for line in obj_text.splitlines():
        if line.startswith("o "):
            current_phase = SIGNAL_OBJECT_PHASE.get(line.removeprefix("o ").strip())
        if line.startswith("usemtl ") and current_phase is not None:
            aspect = line.removeprefix("usemtl ").strip()
            if aspect in SIGNAL_ASPECTS:
                counts[(current_phase, aspect)] += 1
                line = f"usemtl Signal_{current_phase}_{aspect}"
        rewritten.append(line)

    wrong = {key: count for key, count in counts.items() if count != 6}
    if wrong:
        raise RuntimeError(f"unexpected traffic-light material assignments: {wrong}")

    blocks = _material_blocks(mtl_text)
    missing = [aspect for aspect in SIGNAL_ASPECTS if aspect not in blocks]
    if missing:
        raise RuntimeError(f"town MTL is missing signal source materials: {missing}")

    aliases = ["", "# Simulator-only traffic phase materials (generated)."]
    for phase in ("NS", "EW"):
        for aspect in SIGNAL_ASPECTS:
            aliases.append(f"newmtl Signal_{phase}_{aspect}")
            aliases.extend(blocks[aspect])
    return "\n".join(rewritten) + "\n", mtl_text.rstrip() + "\n" + "\n".join(aliases) + "\n"
