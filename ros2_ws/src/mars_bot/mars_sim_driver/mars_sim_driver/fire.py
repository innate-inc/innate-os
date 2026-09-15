"""Blaze's visual fire, driven by the judge's existing timed danger regions.

Flames are RGB-only scene geometry: they cannot block a path, contaminate the
depth map or move a rescue prop. Both renderers consume the same emitters.
This is a visual hazard cue, not a combustion or smoke-transport simulation.
"""

import math

import mujoco
import numpy as np

# One five-minute schedule for free play and Evacuation 1. Its end is also
# the challenge's countdown limit; there is no shorter preview clock.
FIVE_MINUTE_FIRE_S = 300.0
FIVE_MINUTE_REGIONS = (
    # Allow a slow search/pick, then a full minute to return through the
    # west hall and store. Those escape areas and the porch never ignite.
    (240.0, (-3.2, 0.7, -0.35, 2.3), 0.0),
    (270.0, (1.2, -0.5, 3.2, 0.5), 180.0),
    (285.0, (-0.35, 0.7, 3.2, 2.3), 210.0),
    (FIVE_MINUTE_FIRE_S, (0.55, -2.3, 3.2, -0.7), 240.0),
)


def _origin(bounds):
    x0, y0, x1, y1 = bounds
    if x1 < 0:
        return (-1.30, 1.96, 0.25), 0  # stove, away from the medicine
    if y1 < 0:
        return (1.08, -1.82, 0.34), 1  # bed
    if y0 < 0:
        return (2.85, 0.20, 0.02), 2  # east hall
    return (2.03, 1.96, 0.25), 3  # study


class FireEffect:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.reset()

    def reset(self, t: float = 0):
        self._preview_started_t = float(t)
        self.sources = self._spread(FIVE_MINUTE_REGIONS, 0) if self.enabled else []

    def advance(self, t: float):
        """Called once per physics slice; no browser or active trial required."""
        if self.enabled and self._preview_started_t is not None:
            self.sources = self._spread(FIVE_MINUTE_REGIONS, max(0, t - self._preview_started_t))

    def sync(self, challenge, elapsed: float):
        if not self.enabled:
            return
        if challenge is None:
            self.reset()
            return
        from .challenges import After, AnyOf, InRect

        def regions(predicate):
            if isinstance(predicate, AnyOf):
                for child in predicate.preds:
                    yield from regions(child)
            elif isinstance(predicate, After) and isinstance(predicate.inner, InRect):
                if predicate.inner.target == "robot":
                    rect = predicate.inner
                    bounds = (rect.x0, rect.y0, rect.x1, rect.y1)
                    _, kind = _origin(bounds)
                    ignition = 0 if kind == 0 else max(0, predicate.seconds - {1: 60, 2: 90, 3: 75}[kind])
                    yield predicate.seconds, bounds, ignition

        # The judge owns progression until completion/abort; a physics tick
        # must never replace it with the free-play schedule.
        self._preview_started_t = None
        self.sources = self._spread(regions(challenge.fail_if), elapsed)

    @staticmethod
    def _spread(regions, elapsed):
        sources = []
        for deadline, bounds, ignition in regions:
            if elapsed < ignition:
                continue
            origin, kind = _origin(bounds)
            progress = min(1, max(0, (elapsed - ignition) / max(1, deadline - ignition)))
            seed = kind * 17.3  # stable when another region gains emitters
            sources.append([*origin, 0.38 + 0.62 * progress, seed])
            x0, y0, x1, y1 = bounds
            patches = [
                (x0 + (x1 - x0) * (ix + 0.5) / 3, y0 + (y1 - y0) * (iy + 0.5) / 2) for ix in range(3) for iy in range(2)
            ]
            # Spread out from the ignition point, starting early enough to
            # see movement. New patches grow from zero instead of popping in.
            patches.sort(key=lambda point: math.dist(point, origin[:2]))
            for rank, (x, y) in enumerate(patches):
                threshold = 0.12 + 0.11 * rank
                strength = min(1, max(0, (progress - threshold) / (1 - threshold)))
                if strength > 0:
                    sources.append([x, y, 0.025, strength, seed + rank * 2.1 + 1])
        return sources

    def public(self):
        return {"sources": self.sources} if self.enabled else None

    def draw(self, scene, t: float, *, sources=None):
        sources = self.sources if sources is None else sources
        # Transform all faces together: thousands of tiny numpy cross/norm
        # calls here would hold the physics lock for an entire camera frame.
        triangles = [tri for source in sources for tri in flame_triangles(source, t)]
        triangles = triangles[: scene.maxgeom - scene.ngeom]
        if not triangles:
            return
        vertices = np.array([tri[:3] for tri in triangles])
        colors = np.array([tri[3] for tri in triangles], dtype=np.float32)
        ab, ac = vertices[:, 1] - vertices[:, 0], vertices[:, 2] - vertices[:, 0]
        normals = np.cross(ab, ac)
        normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-9)
        frames = np.stack((ab, ac, normals), axis=2).reshape(-1, 9)
        unit = np.ones(3)
        for i in range(len(triangles)):
            geom = scene.geoms[scene.ngeom]
            # mjGEOM_TRIANGLE's local vertices are (0,0), (1,0), (0,1).
            # This affine frame maps them onto the animated flame strip.
            mujoco.mjv_initGeom(geom, mujoco.mjtGeom.mjGEOM_TRIANGLE, unit, vertices[i, 0], frames[i], colors[i])
            geom.emission = 1
            geom.specular = 0
            geom.category = mujoco.mjtCatBit.mjCAT_DECOR
            scene.ngeom += 1
        for source in sources:
            for pos, radius, alpha in smoke_puffs(source, t):
                if scene.ngeom >= scene.maxgeom:
                    return
                geom = scene.geoms[scene.ngeom]
                mujoco.mjv_initGeom(
                    geom,
                    mujoco.mjtGeom.mjGEOM_ELLIPSOID,
                    np.array([radius, radius, radius * 0.7]),
                    np.array(pos),
                    np.eye(3).ravel(),
                    np.array([0.12, 0.105, 0.09, alpha], dtype=np.float32),
                )
                geom.category = mujoco.mjtCatBit.mjCAT_DECOR
                scene.ngeom += 1


def flame_triangles(source, t):
    """Crossed, curved tongues with orange edges and a pale yellow heart.

    Keep the geometry formula in sync with viewer/src/fire.ts. Only rendering
    math is mirrored; region placement and hazard progression live above.
    """
    x, y, z, strength, seed = source
    for tongue in range(3):
        phase = seed + tongue * 2.4
        height = 0.89 * math.sqrt(strength) * (0.80 + 0.20 * math.sin(t * 8 + phase))
        radius = 0.175 * math.sqrt(strength) * (1 if tongue == 0 else 0.7)
        cx = x + math.cos(phase) * radius * 0.55
        cy = y + math.sin(phase) * radius * 0.55
        for layer in range(2):
            h, r = height * (1 if layer == 0 else 0.63), radius * (1 if layer == 0 else 0.48)
            color = (1, 0.18, 0.012, 0.80) if layer == 0 else (1, 0.80, 0.18, 0.95)
            for angle in (phase, phase + math.pi / 2):
                ux, uy = math.cos(angle), math.sin(angle)
                points = []
                for level in range(6):
                    q = level / 5
                    width = r * math.sin(math.pi * (0.18 + 0.82 * q)) * (1 - q) ** 0.35
                    sway = h * 0.18 * math.sin(t * 5 + phase + q * 4) * q * q
                    points.append(
                        [
                            (cx + ux * (sway + side * width), cy + uy * (sway + side * width), z + h * q)
                            for side in (-1, 1)
                        ]
                    )
                for i in range(5):
                    a, b = points[i]
                    c, d = points[i + 1]
                    yield a, b, c, color
                    if i < 4:
                        yield b, d, c, color


def smoke_puffs(source, t):
    x, y, z, strength, seed = source
    for i in range(4):
        age = (t * 0.22 + i / 4 + seed * 0.17) % 1
        radius = 0.06 + age * (0.16 + strength * 0.12)
        pos = (x + math.sin(seed + age * 4) * age * 0.16, y + age * 0.10, z + 0.32 + age * 1.12)
        yield pos, radius, math.sin(math.pi * age) * (0.08 + strength * 0.12) * min(1, strength * 4)
