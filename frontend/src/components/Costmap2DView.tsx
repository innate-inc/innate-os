import { useCallback, useEffect, useRef, useState } from "react";
import styled from "styled-components";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";

type Costmap2DViewProps = {
  wsUrl: string;
};

type RosbridgeMessage = {
  topic?: string;
  msg?: unknown;
};

type OccupancyGridMessage = {
  info?: {
    resolution?: number;
    width?: number;
    height?: number;
    origin?: {
      position?: {
        x?: number;
        y?: number;
      };
    };
  };
  data?: number[];
};

type OdomMessage = {
  pose?: {
    pose?: {
      position?: {
        x?: number;
        y?: number;
      };
      orientation?: {
        x?: number;
        y?: number;
        z?: number;
        w?: number;
      };
    };
  };
};

type MapMetadata = {
  width: number;
  height: number;
  resolution: number;
  originX: number;
  originY: number;
};

const ViewRoot = styled.div`
  position: absolute;
  inset: 0;
  z-index: 14;
`;

const StatusPill = styled.div<{ $isError: boolean }>`
  position: absolute;
  top: 10px;
  left: 10px;
  z-index: 20;
  font-size: 11px;
  text-transform: uppercase;
  padding: 6px 10px;
  letter-spacing: 0.04em;
  border: 1px solid ${({ theme }) => theme.colors.foreground};
  color: ${({ theme }) => theme.colors.foreground};
  background: ${({ $isError }) =>
    $isError ? "rgba(220, 38, 38, 0.85)" : "rgba(0, 0, 0, 0.7)"};
  pointer-events: none;
`;

function quaternionToYaw(x: number, y: number, z: number, w: number): number {
  const sinyCosp = 2 * (w * z + x * y);
  const cosyCosp = 1 - 2 * (y * y + z * z);
  return Math.atan2(sinyCosp, cosyCosp);
}

function createCostmapTexture(
  data: number[],
  width: number,
  height: number,
): THREE.DataTexture {
  const pixelCount = width * height;
  const pixels = new Uint8Array(pixelCount * 4);

  for (let row = 0; row < height; row += 1) {
    for (let col = 0; col < width; col += 1) {
      const sourceIndex = row * width + col;
      const targetRow = height - 1 - row;
      const targetIndex = (targetRow * width + col) * 4;

      const occupancy = data[sourceIndex] ?? -1;
      let shade = 105;
      let alpha = 220;
      if (occupancy >= 0) {
        const clamped = Math.max(0, Math.min(100, occupancy));
        shade = 255 - Math.round((clamped / 100) * 255);
        alpha = 255;
      }

      pixels[targetIndex] = shade;
      pixels[targetIndex + 1] = shade;
      pixels[targetIndex + 2] = shade;
      pixels[targetIndex + 3] = alpha;
    }
  }

  const texture = new THREE.DataTexture(
    pixels,
    width,
    height,
    THREE.RGBAFormat,
  );
  texture.magFilter = THREE.NearestFilter;
  texture.minFilter = THREE.NearestFilter;
  texture.wrapS = THREE.ClampToEdgeWrapping;
  texture.wrapT = THREE.ClampToEdgeWrapping;
  texture.needsUpdate = true;
  return texture;
}

function updateCameraProjection(
  camera: THREE.OrthographicCamera,
  frustumHeight: number,
  width: number,
  height: number,
): void {
  const safeHeight = Math.max(height, 1);
  const aspect = width / safeHeight;
  const halfHeight = frustumHeight * 0.5;
  const halfWidth = halfHeight * aspect;
  camera.left = -halfWidth;
  camera.right = halfWidth;
  camera.top = halfHeight;
  camera.bottom = -halfHeight;
  camera.updateProjectionMatrix();
}

export function Costmap2DView({ wsUrl }: Costmap2DViewProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const rendererRef = useRef<THREE.WebGLRenderer | null>(null);
  const sceneRef = useRef<THREE.Scene | null>(null);
  const cameraRef = useRef<THREE.OrthographicCamera | null>(null);
  const controlsRef = useRef<OrbitControls | null>(null);
  const animationFrameRef = useRef<number | null>(null);
  const frustumHeightRef = useRef(8);
  const mapMetadataRef = useRef<MapMetadata | null>(null);
  const hasFittedMapRef = useRef(false);

  const mapMeshRef = useRef<THREE.Mesh<
    THREE.PlaneGeometry,
    THREE.MeshBasicMaterial
  > | null>(null);
  const mapTextureRef = useRef<THREE.DataTexture | null>(null);
  const robotPointRef = useRef<THREE.Mesh<
    THREE.CircleGeometry,
    THREE.MeshBasicMaterial
  > | null>(null);
  const robotConeRef = useRef<THREE.Mesh<
    THREE.ShapeGeometry,
    THREE.MeshBasicMaterial
  > | null>(null);
  const headingLineRef = useRef<THREE.Line<
    THREE.BufferGeometry,
    THREE.LineBasicMaterial
  > | null>(null);

  const [status, setStatus] = useState("Connecting to map stream");
  const [error, setError] = useState<string | null>(null);

  const fitCameraToMap = useCallback((metadata: MapMetadata) => {
    const camera = cameraRef.current;
    const renderer = rendererRef.current;
    const controls = controlsRef.current;
    if (!camera || !renderer || !controls) {
      return;
    }

    const mapWidthMeters = metadata.width * metadata.resolution;
    const mapHeightMeters = metadata.height * metadata.resolution;
    const centerX = metadata.originX + mapWidthMeters * 0.5;
    const centerY = metadata.originY + mapHeightMeters * 0.5;

    const viewport = renderer.getSize(new THREE.Vector2());
    const safeHeight = Math.max(viewport.y, 1);
    const aspect = viewport.x / safeHeight;
    const paddedWidth = Math.max(mapWidthMeters * 1.2, 1);
    const paddedHeight = Math.max(mapHeightMeters * 1.2, 1);
    const frustumHeight = Math.max(paddedHeight, paddedWidth / aspect);

    frustumHeightRef.current = frustumHeight;
    updateCameraProjection(camera, frustumHeight, viewport.x, viewport.y);
    camera.position.set(centerX, centerY, 25);
    camera.lookAt(centerX, centerY, 0);
    controls.target.set(centerX, centerY, 0);
    controls.update();
  }, []);

  const updateRobotMarker = useCallback((x: number, y: number, yaw: number) => {
    const metadata = mapMetadataRef.current;
    const robotPoint = robotPointRef.current;
    const robotCone = robotConeRef.current;
    const headingLine = headingLineRef.current;

    if (!robotPoint || !robotCone || !headingLine) {
      return;
    }

    const resolution = metadata?.resolution ?? 0.05;
    const pointRadius = Math.max(resolution * 1.8, 0.07);
    const coneLength = Math.max(resolution * 18, 0.85);

    robotPoint.position.set(x, y, 0.04);
    robotPoint.scale.setScalar(pointRadius);

    robotCone.position.set(x, y, 0.03);
    robotCone.rotation.z = yaw;
    robotCone.scale.setScalar(coneLength);

    const endX = x + Math.cos(yaw) * coneLength;
    const endY = y + Math.sin(yaw) * coneLength;
    headingLine.geometry.dispose();
    headingLine.geometry = new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(x, y, 0.05),
      new THREE.Vector3(endX, endY, 0.05),
    ]);
  }, []);

  const applyMapMessage = useCallback(
    (mapMessage: OccupancyGridMessage) => {
      const mapInfo = mapMessage.info;
      const data = mapMessage.data;
      if (
        !mapInfo ||
        !data ||
        typeof mapInfo.width !== "number" ||
        typeof mapInfo.height !== "number" ||
        typeof mapInfo.resolution !== "number"
      ) {
        return;
      }

      const width = mapInfo.width;
      const height = mapInfo.height;
      const resolution = mapInfo.resolution;
      const originX = mapInfo.origin?.position?.x ?? 0;
      const originY = mapInfo.origin?.position?.y ?? 0;

      if (width <= 0 || height <= 0 || resolution <= 0) {
        return;
      }

      const expectedLength = width * height;
      if (data.length < expectedLength) {
        return;
      }

      mapMetadataRef.current = {
        width,
        height,
        resolution,
        originX,
        originY,
      };

      const texture = createCostmapTexture(data, width, height);
      const scene = sceneRef.current;
      if (!scene) {
        texture.dispose();
        return;
      }

      const planeWidth = width * resolution;
      const planeHeight = height * resolution;
      const centerX = originX + planeWidth * 0.5;
      const centerY = originY + planeHeight * 0.5;

      if (!mapMeshRef.current) {
        const geometry = new THREE.PlaneGeometry(planeWidth, planeHeight);
        const material = new THREE.MeshBasicMaterial({
          map: texture,
          transparent: true,
          side: THREE.DoubleSide,
        });
        const mapMesh = new THREE.Mesh(geometry, material);
        mapMesh.position.set(centerX, centerY, 0);
        scene.add(mapMesh);
        mapMeshRef.current = mapMesh;
      } else {
        const mapMesh = mapMeshRef.current;
        mapMesh.geometry.dispose();
        mapMesh.geometry = new THREE.PlaneGeometry(planeWidth, planeHeight);
        mapMesh.position.set(centerX, centerY, 0);

        if (mapTextureRef.current) {
          mapTextureRef.current.dispose();
        }
        mapMesh.material.map = texture;
        mapMesh.material.needsUpdate = true;
      }

      mapTextureRef.current = texture;
      setStatus("map");

      if (!hasFittedMapRef.current) {
        fitCameraToMap(mapMetadataRef.current);
        hasFittedMapRef.current = true;
      }
    },
    [fitCameraToMap],
  );

  const applyOdomMessage = useCallback(
    (odomMessage: OdomMessage) => {
      const pose = odomMessage.pose?.pose;
      if (!pose) {
        return;
      }

      const position = pose.position;
      const orientation = pose.orientation;
      if (!position || !orientation) {
        return;
      }

      const yaw = quaternionToYaw(
        orientation.x ?? 0,
        orientation.y ?? 0,
        orientation.z ?? 0,
        orientation.w ?? 1,
      );

      updateRobotMarker(position.x ?? 0, position.y ?? 0, yaw);
    },
    [updateRobotMarker],
  );

  useEffect(() => {
    const container = containerRef.current;
    if (!container) {
      return;
    }

    const scene = new THREE.Scene();
    scene.background = new THREE.Color("#050505");
    sceneRef.current = scene;

    const size = new THREE.Vector2(
      container.clientWidth || 1,
      container.clientHeight || 1,
    );
    const camera = new THREE.OrthographicCamera(-4, 4, 4, -4, 0.01, 1000);
    camera.position.set(0, 0, 25);
    camera.lookAt(0, 0, 0);
    cameraRef.current = camera;

    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(window.devicePixelRatio || 1);
    renderer.setSize(size.x, size.y);
    rendererRef.current = renderer;
    container.appendChild(renderer.domElement);

    updateCameraProjection(camera, frustumHeightRef.current, size.x, size.y);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableRotate = false;
    controls.screenSpacePanning = true;
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.minZoom = 0.3;
    controls.maxZoom = 20;
    controlsRef.current = controls;

    const grid = new THREE.GridHelper(20, 20, 0x2f2f2f, 0x1a1a1a);
    grid.rotation.x = Math.PI / 2;
    scene.add(grid);

    const pointGeometry = new THREE.CircleGeometry(1, 24);
    const pointMaterial = new THREE.MeshBasicMaterial({ color: 0xff4d4d });
    const robotPoint = new THREE.Mesh(pointGeometry, pointMaterial);
    robotPoint.position.set(0, 0, 0.04);
    scene.add(robotPoint);
    robotPointRef.current = robotPoint;

    const coneShape = new THREE.Shape();
    coneShape.moveTo(0, 0);
    coneShape.lineTo(1, 0.35);
    coneShape.lineTo(1, -0.35);
    coneShape.lineTo(0, 0);

    const coneGeometry = new THREE.ShapeGeometry(coneShape);
    const coneMaterial = new THREE.MeshBasicMaterial({
      color: 0x00b7ff,
      opacity: 0.35,
      transparent: true,
      side: THREE.DoubleSide,
    });
    const robotCone = new THREE.Mesh(coneGeometry, coneMaterial);
    robotCone.position.set(0, 0, 0.03);
    scene.add(robotCone);
    robotConeRef.current = robotCone;

    const headingGeometry = new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(0, 0, 0.05),
      new THREE.Vector3(1, 0, 0.05),
    ]);
    const headingMaterial = new THREE.LineBasicMaterial({
      color: 0x00b7ff,
      linewidth: 1,
    });
    const headingLine = new THREE.Line(headingGeometry, headingMaterial);
    scene.add(headingLine);
    headingLineRef.current = headingLine;

    const animate = () => {
      controls.update();
      renderer.render(scene, camera);
      animationFrameRef.current = window.requestAnimationFrame(animate);
    };
    animate();

    const resizeObserver = new ResizeObserver(() => {
      if (!rendererRef.current || !cameraRef.current || !containerRef.current) {
        return;
      }

      const width = containerRef.current.clientWidth || 1;
      const height = containerRef.current.clientHeight || 1;
      rendererRef.current.setSize(width, height);
      updateCameraProjection(
        cameraRef.current,
        frustumHeightRef.current,
        width,
        height,
      );
    });
    resizeObserver.observe(container);

    return () => {
      resizeObserver.disconnect();

      if (animationFrameRef.current !== null) {
        window.cancelAnimationFrame(animationFrameRef.current);
        animationFrameRef.current = null;
      }

      controls.dispose();

      if (mapMeshRef.current) {
        mapMeshRef.current.geometry.dispose();
        mapMeshRef.current.material.dispose();
        mapMeshRef.current = null;
      }
      if (mapTextureRef.current) {
        mapTextureRef.current.dispose();
        mapTextureRef.current = null;
      }
      if (robotPointRef.current) {
        robotPointRef.current.geometry.dispose();
        robotPointRef.current.material.dispose();
        robotPointRef.current = null;
      }
      if (robotConeRef.current) {
        robotConeRef.current.geometry.dispose();
        robotConeRef.current.material.dispose();
        robotConeRef.current = null;
      }
      if (headingLineRef.current) {
        headingLineRef.current.geometry.dispose();
        headingLineRef.current.material.dispose();
        headingLineRef.current = null;
      }

      renderer.dispose();
      if (renderer.domElement.parentElement === container) {
        container.removeChild(renderer.domElement);
      }

      controlsRef.current = null;
      cameraRef.current = null;
      sceneRef.current = null;
      rendererRef.current = null;
    };
  }, []);

  useEffect(() => {
    setError(null);
    setStatus("Connecting to map stream");

    const ws = new WebSocket(wsUrl);
    let isDisposed = false;

    ws.onopen = () => {
      ws.send(
        JSON.stringify({
          op: "subscribe",
          topic: "/map",
          type: "nav_msgs/msg/OccupancyGrid",
          throttle_rate: 250,
          queue_length: 1,
        }),
      );
      ws.send(
        JSON.stringify({
          op: "subscribe",
          topic: "/odom",
          type: "nav_msgs/msg/Odometry",
          throttle_rate: 100,
          queue_length: 1,
        }),
      );
      setStatus("Waiting for costmap data");
    };

    ws.onmessage = (event) => {
      let payload: unknown;
      try {
        payload = JSON.parse(event.data as string);
      } catch {
        return;
      }

      if (!payload || typeof payload !== "object") {
        return;
      }

      const message = payload as RosbridgeMessage;
      if (!message.topic || message.msg === undefined) {
        return;
      }

      if (message.topic === "/map") {
        applyMapMessage(message.msg as OccupancyGridMessage);
        return;
      }

      if (message.topic === "/odom") {
        applyOdomMessage(message.msg as OdomMessage);
      }
    };

    ws.onerror = () => {
      if (isDisposed) {
        return;
      }
      setError("ROSBridge connection failed");
      setStatus("Map stream unavailable");
    };

    ws.onclose = () => {
      if (isDisposed) {
        return;
      }
      setStatus("Map stream disconnected");
    };

    return () => {
      isDisposed = true;
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ op: "unsubscribe", topic: "/map" }));
        ws.send(JSON.stringify({ op: "unsubscribe", topic: "/odom" }));
      }
      ws.close();
    };
  }, [applyMapMessage, applyOdomMessage, wsUrl]);

  return (
    <ViewRoot ref={containerRef}>
      <StatusPill $isError={Boolean(error)}>{error ?? status}</StatusPill>
    </ViewRoot>
  );
}
