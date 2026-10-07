"""Asynchronous contact selection; workers never command robot hardware."""
import base64
import json
import threading
import time
import traceback
import uuid
from concurrent.futures import Future

import cv2
import numpy as np
from innate_skills.local_target import LocalTarget

PROMPT = ('Select the numbered cross nearest the visible boundary where TWO DIFFERENT socks touch, '
          'with fabric on both sides. Avoid the middle of a single sock, floor, and gaps. '
          'Prefer a green/black sock contact if visible. Return {"id":number}; 0 if none.')


def decode(raw):
    return cv2.imdecode(np.frombuffer(base64.b64decode(raw), np.uint8), cv2.IMREAD_COLOR)


def candidates(raw, encoded_mask, box):
    image = decode(raw)
    mask = cv2.imdecode(np.frombuffer(base64.b64decode(encoded_mask), np.uint8), cv2.IMREAD_GRAYSCALE)
    if image is None or mask is None or mask.shape != image.shape[:2]:
        raise ValueError('Mask and capture dimensions differ')
    count, labels, stats, _ = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8))
    if count < 2:
        raise ValueError('Empty fabric mask')
    mask = (labels == 1 + np.argmax(stats[1:, cv2.CC_STAT_AREA])).astype(np.uint8)
    distance = cv2.distanceTransform(np.pad(mask, 1), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)[1:-1, 1:-1]
    ys, xs = np.nonzero(distance >= 5)
    if not len(xs):
        raise ValueError('No interior fabric pixels')
    x1, y1, x2, y2 = box
    points = []
    for y in np.linspace(y1 + 10, y2 - 10, 3):
        for x in np.linspace(x1 + 10, x2 - 10, 4):
            for i in np.argsort((xs - x) ** 2 + (ys - y) ** 2):
                point = (int(xs[i]), int(ys[i]))
                if all(np.linalg.norm(np.array(point) - p) >= 18 for p in points):
                    points.append(point)
                    break
    for ident, (x, y) in enumerate(points, 1):
        cv2.drawMarker(image, (x, y), (255, 255, 255), cv2.MARKER_CROSS, 10, 1)
        for color, thickness in [((0, 0, 0), 3), ((255, 255, 255), 1)]:
            cv2.putText(image, str(ident), (x + 4, y - 4), cv2.FONT_HERSHEY_SIMPLEX, .45, color, thickness, cv2.LINE_AA)
    ok, jpeg = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not ok:
        raise ValueError('Cannot encode candidates')
    return np.array(points, dtype=float), jpeg.tobytes()


def select(provider, jpeg):
    from innate_llm import Image, Message, Request, Role, Text, Thinking
    reply = provider.run(Request(system='', messages=(Message(Role.USER, (Text(PROMPT), Image(jpeg))),),
                                 temperature=0, thinking=Thinking.MINIMAL, max_tokens=16), timeout=5)
    result = json.loads(reply.message.text())
    ident = result.get('id')
    if type(ident) is not int:
        raise ValueError('Invalid candidate ID')
    return ident


def error_details(exc):
    # Do not log HTTP bodies, credentials, image payloads, or exception messages.
    frames = traceback.extract_tb(exc.__traceback__)
    return dict(error=type(exc).__name__,
                location=" > ".join(f"{f.name}:{f.lineno}" for f in frames[-4:]))


class ContactSelection:
    def __init__(self, raw, mask, box, provider, read_image, logger):
        self.points, jpeg = candidates(raw, mask, box)
        gray = cv2.cvtColor(decode(raw), cv2.COLOR_BGR2GRAY)
        self.tracker = LocalTarget(gray, self.points.mean(axis=0), box)
        self.raw = raw
        self.read_image = read_image
        self.logger = logger
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.valid = True
        self.started = self.last_frame = time.monotonic()
        self.future = Future()
        self.taken = False
        self.trace_id = uuid.uuid4().hex[:8]
        self.reason = None
        self.wait_logged = False
        self.log('request_started', candidates=len(self.points), jpeg_bytes=len(jpeg), timeout_s=5)
        def request():
            try:
                ident = select(provider, jpeg)
                self.log('request_completed', candidate_id=ident, late=self.closed.is_set())
                self.future.set_result(ident)
            except Exception as exc:
                self.log('request_failed', **error_details(exc), late=self.closed.is_set())
                self.future.set_exception(exc)
        threading.Thread(target=request, daemon=True, name='sock-contact-qwen').start()
        self.worker = threading.Thread(target=self._track, daemon=True, name='sock-contact-track')
        self.worker.start()

    def log(self, event, **fields):
        fields.update(event=event, request_id=self.trace_id,
                      elapsed_s=round(time.monotonic()-self.started, 3))
        self.logger.info('[SockContact] ' + json.dumps(fields, allow_nan=False))

    def _invalidate(self, reason, **fields):
        if self.valid:
            self.reason = reason
            self.log('fallback', reason=reason, **fields)
        self.valid = False

    def close(self):
        if not self.closed.is_set():
            self.log('closed', consumed=self.taken, tracking_valid=self.valid,
                     request_done=self.future.done(), reason=self.reason)
        self.closed.set()
        self.worker.join(timeout=.5)

    def _update(self, raw):
        now = time.monotonic()
        if now - self.last_frame > .5:
            self._invalidate('frame_gap', frame_gap_s=round(now-self.last_frame, 3))
            return
        if raw is self.raw:
            return
        image = decode(raw)
        if image is None or self.tracker.track(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)) is None:
            self._invalidate('image_decode_failed' if image is None else 'visual_correspondence_lost')
            return
        transform = self.tracker.last_transform
        self.points = np.c_[self.points, np.ones(len(self.points))] @ transform.T
        self.raw, self.last_frame = raw, now

    def _track(self):
        while not self.closed.wait(.06):
            try:
                with self.lock:
                    if not self.valid or self.taken:
                        return
                    if time.monotonic() - self.started > 20:
                        self._invalidate('tracking_deadline')
                        return
                    raw = self.read_image()
                    if raw:
                        self._update(raw)
            except Exception as exc:
                with self.lock:
                    self._invalidate('tracking_exception', **error_details(exc))
                return

    def take(self, raw, arrived=False):
        """Return ('point', (pixel, box)), ('wait', None), or None fallback."""
        with self.lock:
            if self.closed.is_set() or self.taken or not self.valid:
                return None
            self._update(raw)
            if not self.valid:
                return None
            if not self.future.done():
                waiting = arrived and time.monotonic() - self.started < 5.5
                if arrived and not self.wait_logged:
                    self.log('arrival_wait' if waiting else 'fallback', reason='request_pending')
                    self.wait_logged = True
                return ('wait', None) if waiting else None
            self.taken = True
            try:
                ident = self.future.result()
            except Exception as exc:
                self.log('fallback', reason='request_failed', **error_details(exc))
                return None
            if not 1 <= ident <= len(self.points):
                self.log('fallback', reason='no_contact' if ident == 0 else 'invalid_candidate_id', candidate_id=ident)
                return None
            point = self.points[ident - 1]
            h, w = self.tracker.gray.shape
            if not np.isfinite(point).all() or not (0 <= point[0] < w and 0 <= point[1] < h):
                self.log('fallback', reason='point_outside_frame', candidate_id=ident)
                return None
            self.log('candidate_ready', candidate_id=ident, current_pixel=point.tolist())
            return 'point', (tuple(point), self.tracker.box)
