"""Feature basket memory shared by pickup and drop; no robot motion."""
import base64
import json
import math
import os
from pathlib import Path
import time

import cv2
import numpy as np

from innate.exceptions import SkillFailed
from innate.geometry import pixel_to_floor
from innate_skills.sock_box_exclusion import projected_hull, transform_vertices

CACHE = Path('/tmp/innate-feature-basket.json')
BOOT = Path('/proc/sys/kernel/random/boot_id')
MARGIN = 0.10


def odom_xy(host):
    o = getattr(host, "odom", None)
    if (o is None or getattr(o, 'frame_id', None) != 'odom' or
            not all(math.isfinite(v) for v in (o.x, o.y, o.theta, o.stamp)) or
            not 0 <= time.time() - o.stamp < 0.5):
        raise SkillFailed('Fresh odometry required for basket exclusion')
    return o.x, o.y, o.theta


def remember(host, result, tilt):
    from innate_skills.box_features import candidate_face_in_base
    candidate = None
    for item in [result.get('best', {}), *result.get('candidates', [])]:
        try:
            center, normal = candidate_face_in_base(item, tilt)
            width = float(item['face_width_m'])
            if not math.isfinite(width) or width <= 0:
                continue
            candidate = item
            break
        except (KeyError, ValueError, TypeError):
            continue
    if candidate is None:
        return None
    # The physical basket is 50 x 33 cm. Its short and long faces can match.
    width, depth = (0.50, 0.33) if width > 0.40 else (0.33, 0.50)
    right = np.array([normal[1], -normal[0]])
    vertices = np.array([
        [*(center + right * r + normal * d), z]
        for r in (-width / 2 - MARGIN, width / 2 + MARGIN)
        for d in (-MARGIN, depth + MARGIN)
        for z in (0., 0.25)
    ])
    world = transform_vertices(vertices, odom_xy(host))
    payload = {'boot': BOOT.read_text().strip(), 'time': time.time(), 'vertices': world.tolist()}
    tmp = CACHE.with_suffix('.'+str(os.getpid())+'.tmp')
    tmp.write_text(json.dumps(payload));os.replace(tmp, CACHE)
    host.logger.info(f"[FeatureSockBox] saved {candidate.get('reference')} with 10cm margin")
    return world


def recall():
    try:
        data = json.loads(CACHE.read_text())
        vertices = np.asarray(data['vertices'], dtype=float)
        if (data['boot'] != BOOT.read_text().strip() or vertices.shape != (8, 3)
                or not np.isfinite(vertices).all() or not 0 <= time.time()-data['time'] < 30):
            return None
        return vertices
    except (OSError, ValueError, TypeError, KeyError):
        return None


class FeatureBasketExclusion:
    def __init__(self, host):
        from innate_skills.box_features import FeatureObserver
        self.host = host
        self.observer = FeatureObserver()
        self.world_vertices = recall()
        self.reuse_drop_pose = self.world_vertices is not None

    def close(self):
        self.observer.close()

    def refresh(self, image):
        if self.reuse_drop_pose:
            self.reuse_drop_pose = False
            self.host.logger.info('[FeatureSockBox] using recent shared basket pose; no API call')
            return
        before = odom_xy(self.host)
        self.host.check_cancelled()
        try:
            result = self.observer.capture(base64.b64decode(image))
        except Exception as error:
            if self.world_vertices is None:
                raise SkillFailed(f'Basket exclusion API unavailable with no remembered basket: {error}') from error
            self.host.logger.info(f'[FeatureSockBox] API unavailable; retaining known basket: {error}')
            return
        self.host.check_cancelled()
        after = odom_xy(self.host)
        # Same stationary-observation limits as the feature drop observer.
        if math.hypot(after[0]-before[0], after[1]-before[1]) > .01 or abs(math.atan2(math.sin(after[2]-before[2]),math.cos(after[2]-before[2]))) > .02:
            raise SkillFailed('Robot moved during basket exclusion observation')
        world = remember(self.host, result, self.host._p['tilt_deg']) if result.get('detected') else None
        if world is not None:
            self.world_vertices = world
        self.host.logger.info(f"[FeatureSockBox] detected={result.get('detected')} remembered={self.world_vertices is not None}")

    def vertices(self):
        return (transform_vertices(self.world_vertices, odom_xy(self.host), inverse=True)
                if self.world_vertices is not None else None)

    def hull(self, image=None):
        vertices = self.vertices()
        return projected_hull(vertices, self.host._p['tilt_deg']) if vertices is not None else None

    @staticmethod
    def contains(hull, point):
        return hull is not None and cv2.pointPolygonTest(hull, tuple(map(float,point)), False) >= 0

    def filter(self, candidates, image):
        self.refresh(image)
        vertices = self.vertices()
        if vertices is None:
            self.host.logger.info('[FeatureSockBox] basket position unknown; keep searching before selecting socks')
            return []
        hull = projected_hull(vertices, self.host._p['tilt_deg']) if vertices is not None else None
        footprint = cv2.convexHull(vertices[:, :2].astype(np.float32)) if vertices is not None else None
        kept=[]
        for candidate in candidates:
            u,v,_,box=candidate
            floor=pixel_to_floor(u,v,self.host._p['tilt_deg'])
            excluded=self.contains(hull,(u,v)) or (floor is not None and self.contains(footprint,floor))
            if hull is not None:
                x1,y1,x2,y2=box
                rect=np.array([[x1,y1],[x2,y1],[x2,y2],[x1,y2]],dtype=np.float32)
                overlap,_=cv2.intersectConvexConvex(hull,rect)
                excluded |= overlap > 0
            if not excluded:kept.append(candidate)
        self.host.logger.info(f'[FeatureSockBox] raw={len(candidates)} excluded={len(candidates)-len(kept)} kept={len(kept)}')
        return kept
