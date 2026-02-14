import { useCallback, useEffect, useRef, useState } from "react";

const WEBRTC_START_TOPIC = "/webrtc/start";
const WEBRTC_OFFER_TOPIC = "/webrtc/offer";
const WEBRTC_ANSWER_TOPIC = "/webrtc/answer";
const WEBRTC_ICE_IN_TOPIC = "/webrtc/ice_in";
const WEBRTC_ICE_OUT_TOPIC = "/webrtc/ice_out";

type WebRTCSource = "live" | "episode_replay";

interface UseRobotWebRTCOptions {
  enabled: boolean;
  wsUrl: string;
  source?: WebRTCSource;
}

interface UseRobotWebRTCReturn {
  mainStream: MediaStream | null;
  secondaryStream: MediaStream | null;
  hasMedia: boolean;
  isConnecting: boolean;
  error: string | null;
  reconnect: () => void;
}

const CONNECTION_TIMEOUT_MS = 30000;
const START_SIGNAL_DELAY_MS = 100;

export function useRobotWebRTC({
  enabled,
  wsUrl,
  source = "live",
}: UseRobotWebRTCOptions): UseRobotWebRTCReturn {
  const [mainStream, setMainStream] = useState<MediaStream | null>(null);
  const [secondaryStream, setSecondaryStream] = useState<MediaStream | null>(
    null,
  );
  const [hasMedia, setHasMedia] = useState(false);
  const [isConnecting, setIsConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reconnectCount, setReconnectCount] = useState(0);
  const hasMediaRef = useRef(false);

  const reconnect = useCallback(() => {
    setReconnectCount((count) => count + 1);
  }, []);

  useEffect(() => {
    hasMediaRef.current = hasMedia;
  }, [hasMedia]);

  useEffect(() => {
    if (!enabled) {
      setMainStream(null);
      setSecondaryStream(null);
      setHasMedia(false);
      setIsConnecting(false);
      setError(null);
      return;
    }

    let isMounted = true;
    let ws: WebSocket | null = null;
    let pc: RTCPeerConnection | null = null;
    let connectionTimeout: number | null = null;
    let startSignalTimer: number | null = null;
    let processingOffer = false;
    let remoteDescriptionSet = false;
    const iceCandidateQueue: RTCIceCandidateInit[] = [];
    let videoTrackCount = 0;

    const clearTimers = () => {
      if (connectionTimeout !== null) {
        window.clearTimeout(connectionTimeout);
        connectionTimeout = null;
      }
      if (startSignalTimer !== null) {
        window.clearTimeout(startSignalTimer);
        startSignalTimer = null;
      }
    };

    const closeConnections = () => {
      clearTimers();

      if (pc) {
        pc.ontrack = null;
        pc.onicecandidate = null;
        pc.oniceconnectionstatechange = null;
        pc.close();
        pc = null;
      }

      if (ws) {
        ws.onopen = null;
        ws.onmessage = null;
        ws.onerror = null;
        ws.onclose = null;
        ws.close();
        ws = null;
      }
    };

    const sendRosbridgeMessage = (payload: object) => {
      if (ws?.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(payload));
      }
    };

    const publishTopic = (topic: string, msg: object) => {
      sendRosbridgeMessage({ op: "publish", topic, msg });
    };

    const createPeerConnection = () => {
      pc = new RTCPeerConnection({
        iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
      });

      videoTrackCount = 0;
      remoteDescriptionSet = false;
      iceCandidateQueue.length = 0;

      pc.onicecandidate = (event) => {
        if (!event.candidate) {
          return;
        }

        publishTopic(WEBRTC_ICE_IN_TOPIC, {
          data: JSON.stringify({
            candidate: event.candidate.candidate,
            sdpMLineIndex: event.candidate.sdpMLineIndex,
            sdpMid: event.candidate.sdpMid,
          }),
        });
      };

      pc.ontrack = (event) => {
        if (!isMounted || event.track.kind !== "video") {
          return;
        }

        const stream = new MediaStream([event.track]);
        const mid = event.transceiver?.mid;
        const isMainMid = mid === "0" || mid === "video0";
        const isSecondaryMid = mid === "1" || mid === "video1";

        if (isMainMid) {
          setMainStream(stream);
        } else if (isSecondaryMid) {
          setSecondaryStream(stream);
          setMainStream((current) => current ?? stream);
        } else {
          videoTrackCount += 1;
          if (videoTrackCount === 1) {
            setMainStream(stream);
          } else if (videoTrackCount === 2) {
            setSecondaryStream(stream);
          }
        }

        setHasMedia(true);
        setIsConnecting(false);
        setError(null);
        clearTimers();
      };

      pc.oniceconnectionstatechange = () => {
        if (!pc || !isMounted) {
          return;
        }
      };

      pc.onsignalingstatechange = () => {
        if (!pc) {
          return;
        }
      };
    };

    const processOffer = async (offerSdp: string) => {
      if (!pc || processingOffer || pc.signalingState !== "stable") {
        return;
      }

      processingOffer = true;
      try {
        await pc.setRemoteDescription({ type: "offer", sdp: offerSdp });
        remoteDescriptionSet = true;

        for (const candidate of iceCandidateQueue) {
          try {
            await pc.addIceCandidate(candidate);
          } catch {
            // Ignore malformed ICE candidates.
          }
        }
        iceCandidateQueue.length = 0;

        const answer = await pc.createAnswer();
        await pc.setLocalDescription(answer);
        publishTopic(WEBRTC_ANSWER_TOPIC, { data: answer.sdp ?? "" });
      } catch (offerError) {
        console.error("[WebRTC] Failed to process offer:", offerError);
      } finally {
        processingOffer = false;
      }
    };

    const processIceOut = async (rawIceData: string) => {
      if (!pc) {
        return;
      }

      try {
        const parsed = JSON.parse(rawIceData) as {
          candidate?: string;
          sdpMLineIndex?: number;
          sdpMid?: string;
        };

        if (!parsed.candidate) {
          return;
        }

        const candidate: RTCIceCandidateInit = {
          candidate: parsed.candidate,
          sdpMLineIndex: parsed.sdpMLineIndex ?? 0,
          sdpMid: parsed.sdpMid ?? undefined,
        };

        if (!remoteDescriptionSet) {
          iceCandidateQueue.push(candidate);
          return;
        }

        await pc.addIceCandidate(candidate);
      } catch {
        // ICE parsing failures are usually transient and can be ignored.
      }
    };

    const connectWebSocket = () => {
      closeConnections();
      setMainStream(null);
      setSecondaryStream(null);
      setHasMedia(false);
      setError(null);
      setIsConnecting(true);

      connectionTimeout = window.setTimeout(() => {
        if (!isMounted) {
          return;
        }
        setError("Timed out while waiting for WebRTC media from robot.");
        setIsConnecting(false);
      }, CONNECTION_TIMEOUT_MS);

      ws = new WebSocket(wsUrl);

      ws.onopen = () => {
        if (!isMounted) {
          return;
        }

        createPeerConnection();

        sendRosbridgeMessage({ op: "subscribe", topic: WEBRTC_OFFER_TOPIC });
        sendRosbridgeMessage({ op: "subscribe", topic: WEBRTC_ICE_OUT_TOPIC });

        startSignalTimer = window.setTimeout(() => {
          const sourceForRobot =
            source === "episode_replay" ? "replay" : source;
          publishTopic(WEBRTC_START_TOPIC, {
            data: JSON.stringify({ source: sourceForRobot }),
          });
          startSignalTimer = null;
        }, START_SIGNAL_DELAY_MS);
      };

      ws.onmessage = async (event) => {
        try {
          const data = JSON.parse(event.data as string);
          if (data.op !== "publish" || typeof data.topic !== "string") {
            return;
          }

          if (data.topic === WEBRTC_OFFER_TOPIC) {
            const offerSdp = data.msg?.data ?? data.data;
            if (typeof offerSdp === "string" && offerSdp.length > 0) {
              await processOffer(offerSdp);
            }
            return;
          }

          if (data.topic === WEBRTC_ICE_OUT_TOPIC) {
            const rawIceData = data.msg?.data ?? data.data;
            if (typeof rawIceData === "string" && rawIceData.length > 0) {
              await processIceOut(rawIceData);
            }
          }
        } catch {
          // Ignore non-JSON messages from ROSBridge.
        }
      };

      ws.onerror = () => {
        if (!isMounted) {
          return;
        }
        setError("Failed to connect to robot signaling WebSocket.");
      };

      ws.onclose = () => {
        if (!isMounted) {
          return;
        }

        if (!hasMediaRef.current) {
          setError("Robot signaling WebSocket closed.");
          setIsConnecting(false);
        }
      };
    };

    connectWebSocket();

    return () => {
      isMounted = false;
      closeConnections();
      setMainStream(null);
      setSecondaryStream(null);
      setHasMedia(false);
      setIsConnecting(false);
    };
  }, [enabled, wsUrl, source, reconnectCount]);

  return {
    mainStream,
    secondaryStream,
    hasMedia,
    isConnecting,
    error,
    reconnect,
  };
}
