/**
 * Choose the world point shown at the canvas centre. A following thumbnail
 * deliberately ignores a stale full-map pan.
 * @param {{ x: number, y: number, yaw: number } | null} pose
 * @param {{ x: number, y: number } | null} panCenter
 * @param {number | undefined} zoomMeters
 * @param {boolean} followRobot
 */
export function mapViewCenter(pose, panCenter, zoomMeters, followRobot) {
  return (
    (followRobot && zoomMeters && pose ? pose : null) ??
    panCenter ??
    (zoomMeters && pose ? pose : null)
  );
}
