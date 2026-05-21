import { useState, useEffect, useRef, useCallback } from "react";
import { IoMic, IoMicOff, IoRefresh } from "react-icons/io5";
import styled from "styled-components";
import "./App.css";
import { ImageDisplay } from "./components/ImageDisplay";
import { Chat } from "./components/Chat";
import {
  AvailableAgentsResponse,
  BrainBackendStatus,
  RobotAgent,
  StackMetricsResponse,
  getAvailableAgentsDirect,
  resetBrainDirect,
  setBrainActiveDirect,
  setBrainBackendConfigDirect,
  setDirectiveDirect,
} from "./services/rosbridgeService";

const AGENT_BACKEND_WARNING_DELAY_MS = 15_000;
const BACKEND_CONNECTED_STABLE_MS = 3_000;
const BACKEND_STATUS_POLL_MS = 1_000;
const BRAIN_URI_URL_PARAMS = ["brain_uri", "brain_websocket_uri", "websocket_uri"];
const SERVICE_KEY_URL_PARAMS = ["innate_service_key", "service_key"];
const BACKEND_OVERRIDE_URL_PARAMS = [
  ...BRAIN_URI_URL_PARAMS,
  ...SERVICE_KEY_URL_PARAMS,
];
const BACKEND_WARMUP_STATES = new Set([
  "unknown",
  "starting",
  "configured",
  "connecting",
  "authenticating",
]);

function formatBackendState(state?: string | null) {
  return (state || "unknown").replace(/_/g, " ");
}

function isBackendWarningStatus(
  status: BrainBackendStatus | null,
  timedOut: boolean,
) {
  if (!status) {
    return false;
  }

  if (status.connected) {
    return timedOut && !isBackendConnectedStable(status);
  }

  const state = status.state || "unknown";
  return timedOut || !BACKEND_WARMUP_STATES.has(state);
}

function isBackendConnectedStable(status: BrainBackendStatus | null) {
  if (!status?.connected) {
    return false;
  }

  const timestamp = status.timestamp ?? status.updated_at;
  if (typeof timestamp !== "number") {
    return true;
  }

  return Date.now() - timestamp * 1000 >= BACKEND_CONNECTED_STABLE_MS;
}

type BackendDisplayLevel = "healthy" | "warning" | "error";

function backendDisplayLevel(status: BrainBackendStatus | null): BackendDisplayLevel {
  if (status?.connected) {
    return "healthy";
  }

  const state = status?.state || "unknown";
  if (state === "invalid_config" || state === "connection_error") {
    return "error";
  }
  if (state === "backend_error" || state === "disconnected" || state === "stopped") {
    return "error";
  }
  return "warning";
}

function backendDisplayLabel(status: BrainBackendStatus | null) {
  if (status?.connected) {
    return "connected";
  }

  const state = status?.state || "unknown";
  const message = status?.message || "";
  if (state === "invalid_config" && message.toUpperCase().includes("KEY")) {
    return "missing key";
  }
  if (BACKEND_WARMUP_STATES.has(state)) {
    return "connecting";
  }
  return formatBackendState(state);
}

function firstUrlParam(params: URLSearchParams, names: string[]) {
  for (const name of names) {
    const value = params.get(name);
    if (value?.trim()) {
      return value.trim();
    }
  }
  return undefined;
}

function backendParamsFromUrl() {
  const searchParams = new URLSearchParams(window.location.search);
  const rawHash = window.location.hash.replace(/^#\??/, "");
  const hashParams = rawHash.includes("=")
    ? new URLSearchParams(rawHash)
    : new URLSearchParams();
  const websocket_uri =
    firstUrlParam(searchParams, BRAIN_URI_URL_PARAMS) ||
    firstUrlParam(hashParams, BRAIN_URI_URL_PARAMS);
  const service_key =
    firstUrlParam(searchParams, SERVICE_KEY_URL_PARAMS) ||
    firstUrlParam(hashParams, SERVICE_KEY_URL_PARAMS);

  if (!websocket_uri && !service_key) {
    return null;
  }
  return { websocket_uri, service_key };
}

function stripBackendParamsFromUrl() {
  const url = new URL(window.location.href);
  let changed = false;
  for (const name of BACKEND_OVERRIDE_URL_PARAMS) {
    if (url.searchParams.has(name)) {
      url.searchParams.delete(name);
      changed = true;
    }
  }

  const rawHash = url.hash.replace(/^#\??/, "");
  if (rawHash.includes("=")) {
    const hashParams = new URLSearchParams(rawHash);
    for (const name of BACKEND_OVERRIDE_URL_PARAMS) {
      if (hashParams.has(name)) {
        hashParams.delete(name);
        changed = true;
      }
    }
    const nextHash = hashParams.toString();
    url.hash = nextHash ? `#${nextHash}` : "";
  }

  if (changed) {
    window.history.replaceState({}, document.title, url.toString());
  }
}

// Main App Container
const AppContainer = styled.div`
  display: grid;
  grid-template-rows: auto 1fr auto;
  height: 100%;
  border: 1px solid ${({ theme }) => theme.colors.foreground};
  margin: 20px;
  max-width: 1600px;
  align-self: center;
  width: calc(100% - 40px);

  @media (max-width: 1024px) {
    margin: 0;
    border: none;
    width: 100%;
    max-width: 100%;
    overflow-x: hidden;
  }
`;

// Header
const Header = styled.header`
  display: grid;
  grid-template-columns: 250px 1fr auto;
  border-bottom: 1px solid ${({ theme }) => theme.colors.foreground};
  height: 60px;
  align-items: center;

  @media (max-width: 1024px) {
    display: flex;
    justify-content: space-between;
    padding: 0;
  }
`;

const Logo = styled.div`
  font-family: ${({ theme }) => theme.fonts.display};
  font-size: 24px;
  font-weight: 800;
  padding: 0 16px;
  letter-spacing: -0.02em;
  display: flex;
  align-items: center;
  height: 100%;
  border-right: 1px solid ${({ theme }) => theme.colors.foreground};

  @media (max-width: 1024px) {
    border-right: none;
    font-size: 20px;
    padding: 0 8px;
  }
`;

const StatusBadge = styled.div<{ $level: BackendDisplayLevel }>`
  margin-right: 16px;
  background: ${({ $level, theme }) => {
    if ($level === "healthy") {
      return theme.colors.success;
    }
    if ($level === "error") {
      return theme.colors.error;
    }
    return theme.colors.primary;
  }};
  color: white;
  padding: 6px 16px;
  border-radius: 20px;
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  display: flex;
  align-items: center;
  gap: 8px;
`;

const StatusDot = styled.div`
  width: 8px;
  height: 8px;
  background: #fff;
  border-radius: 50%;
  animation: pulse 2s infinite;
`;

const HamburgerButton = styled.button`
  display: none;
  background: none;
  border: none;
  color: white;
  padding: 8px;
  margin-left: 8px;
  cursor: pointer;

  @media (max-width: 1024px) {
    display: flex;
    align-items: center;
  }
`;

const DrawerOverlay = styled.div<{ $isOpen: boolean }>`
  display: none;

  @media (max-width: 1024px) {
    display: block;
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.8);
    z-index: 60;
    opacity: ${({ $isOpen }) => ($isOpen ? 1 : 0)};
    pointer-events: ${({ $isOpen }) => ($isOpen ? "auto" : "none")};
    transition: opacity 0.3s;
  }
`;

const DrawerHeader = styled.div`
  display: none;

  @media (max-width: 1024px) {
    display: flex;
    height: 60px;
    align-items: center;
    justify-content: space-between;
    padding: 0 16px;
    border-bottom: 1px solid ${({ theme }) => theme.colors.foreground};
    background: ${({ theme }) => theme.colors.background};
  }
`;

const DrawerCloseButton = styled.button`
  background: none;
  border: none;
  color: ${({ theme }) => theme.colors.foreground};
  opacity: 0.5;
  cursor: pointer;
  padding: 4px;

  &:hover {
    opacity: 1;
  }
`;

// Workspace
const Workspace = styled.div`
  display: grid;
  grid-template-columns: 300px 1fr 450px;
  overflow: hidden;

  @media (max-width: 1200px) {
    grid-template-columns: 250px 1fr 390px;
  }

  @media (max-width: 1024px) {
    display: flex;
    flex-direction: column;
    grid-template-columns: none;
    width: 100%;
    max-width: 100%;
  }
`;

// Left Sidebar
const Sidebar = styled.aside<{ $isOpen?: boolean }>`
  border-right: 1px solid ${({ theme }) => theme.colors.foreground};
  display: flex;
  flex-direction: column;
  overflow-y: auto;
  background: ${({ theme }) => theme.colors.background};

  @media (max-width: 1024px) {
    position: fixed;
    top: 0;
    left: 0;
    bottom: 0;
    width: 300px;
    z-index: 70;
    transform: ${({ $isOpen }) =>
      $isOpen ? "translateX(0)" : "translateX(-100%)"};
    transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    box-shadow: ${({ $isOpen }) =>
      $isOpen ? "0 0 30px rgba(0,0,0,0.5)" : "none"};
  }
`;

const PanelSection = styled.div`
  border-bottom: 1px solid ${({ theme }) => theme.colors.foreground};
`;

const PanelHeader = styled.div`
  padding: 12px 16px;
  font-size: 11px;
  text-transform: uppercase;
  border-bottom: 1px solid ${({ theme }) => theme.colors.foreground};
  font-weight: 700;
  opacity: 0.7;
`;

const PanelHeaderRow = styled(PanelHeader)`
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
`;

const AgentReloadButton = styled.button<{ $isLoading?: boolean }>`
  width: 26px;
  height: 26px;
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  color: ${({ theme }) => theme.colors.foreground};
  border: 1px solid ${({ theme }) => theme.colors.foreground};
  cursor: pointer;
  opacity: ${({ disabled }) => (disabled ? 0.35 : 0.85)};
  transition:
    background 0.1s,
    color 0.1s,
    opacity 0.1s;

  svg {
    animation: ${({ $isLoading }) =>
      $isLoading ? "spin 0.8s linear infinite" : "none"};
  }

  &:hover:not(:disabled) {
    background: ${({ theme }) => theme.colors.foreground};
    color: ${({ theme }) => theme.colors.background};
    opacity: 1;
  }
`;

const BigStat = styled.div`
  padding: 16px;
`;

const StatValue = styled.div`
  font-family: ${({ theme }) => theme.fonts.display};
  font-size: 48px;
  line-height: 0.9;
  font-weight: 400;
  letter-spacing: -0.05em;
`;

const StatLabel = styled.div`
  font-size: 12px;
  margin-top: 8px;
  opacity: 0.6;
`;

const AgentItem = styled.div<{ $isActive: boolean }>`
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px;
  border-bottom: 1px solid ${({ theme }) => theme.colors.foreground};
  cursor: pointer;
  transition: background 0.2s;
  background: ${({ $isActive, theme }) =>
    $isActive ? theme.colors.foreground : "transparent"};
  color: ${({ $isActive, theme }) =>
    $isActive ? theme.colors.background : theme.colors.foreground};

  &:hover {
    background: ${({ $isActive, theme }) =>
      $isActive ? theme.colors.foreground : "rgba(255, 255, 255, 0.1)"};
  }
`;

const AgentStatusItem = styled.div`
  padding: 12px 16px;
  border-bottom: 1px solid ${({ theme }) => theme.colors.foreground};
  font-size: 13px;
  font-weight: 500;
  opacity: 0.65;
`;

const BackendStatusItem = styled.div<{ $level: BackendDisplayLevel }>`
  padding: 12px 16px;
  border-bottom: 1px solid ${({ theme }) => theme.colors.foreground};
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  font-size: 12px;
`;

const BackendStatusLabel = styled.span`
  opacity: 0.6;
`;

const BackendStatusValue = styled.span<{ $level: BackendDisplayLevel }>`
  color: ${({ $level, theme }) => {
    if ($level === "healthy") {
      return theme.colors.success;
    }
    if ($level === "error") {
      return theme.colors.error;
    }
    return theme.colors.primaryHover;
  }};
  font-weight: 700;
  text-transform: uppercase;
`;

const AgentName = styled.span`
  font-size: 13px;
  font-weight: 500;
`;

const AgentNotice = styled.div`
  margin: 12px 16px;
  padding: 12px;
  border: 1px solid ${({ theme }) => theme.colors.error};
  background: rgba(255, 59, 59, 0.12);
  color: ${({ theme }) => theme.colors.foreground};
`;

const AgentNoticeTitle = styled.div`
  color: ${({ theme }) => theme.colors.error};
  font-size: 11px;
  font-weight: 700;
  line-height: 1.3;
  text-transform: uppercase;
  margin-bottom: 6px;
`;

const AgentNoticeDetail = styled.div`
  font-size: 12px;
  line-height: 1.45;
  opacity: 0.85;
  word-break: break-word;
`;

const AgentCheck = styled.div<{ $isActive: boolean }>`
  width: 16px;
  height: 16px;
  border: 1px solid
    ${({ $isActive, theme }) =>
      $isActive ? theme.colors.background : "currentColor"};
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  background: ${({ $isActive, theme }) =>
    $isActive ? theme.colors.primary : "transparent"};
  color: ${({ $isActive }) => ($isActive ? "white" : "transparent")};
  font-size: 10px;
`;

// Main Content - centers the 4:3 viewport
const MainContent = styled.main`
  display: flex;
  flex-direction: column;
  position: relative;
  background: #0a0a0a;
  overflow: hidden;
  align-items: center;
  justify-content: center;

  @media (max-width: 1024px) {
    height: 35vh;
    flex-shrink: 0;
    width: 100%;
    min-width: 0;
  }
`;

// Chat Column
const ChatColumn = styled.aside`
  border-left: 1px solid ${({ theme }) => theme.colors.foreground};
  display: flex;
  flex-direction: column;
  background: ${({ theme }) => theme.colors.background};
  overflow: hidden;

  @media (max-width: 1024px) {
    display: flex;
    border-left: none;
    border-top: 1px solid ${({ theme }) => theme.colors.foreground};
    min-height: 0;
    flex: 1;
    width: 100%;
    min-width: 0;
  }
`;

// Footer
const Footer = styled.footer`
  border-top: 1px solid ${({ theme }) => theme.colors.foreground};
  height: 100px;
  display: grid;
  grid-template-columns: 1fr 450px;

  @media (max-width: 1200px) {
    grid-template-columns: 1fr 390px;
  }

  @media (max-width: 1024px) {
    height: auto;
    grid-template-columns: 1fr;
    padding-bottom: env(safe-area-inset-bottom, 12px);
  }
`;

const ControlPanel = styled.div`
  padding: 16px;
  display: flex;
  gap: 20px;
  align-items: center;

  @media (max-width: 1024px) {
    display: none;
  }
`;

const ActionButton = styled.button<{ $isDanger?: boolean }>`
  height: 50px;
  padding: 0 24px;
  white-space: nowrap;
  font-family: ${({ theme }) => theme.fonts.mono};
  font-size: 12px;
  text-transform: uppercase;
  font-weight: 700;
  background: ${({ theme }) => theme.colors.background};
  color: ${({ $isDanger, theme }) =>
    $isDanger ? theme.colors.error : theme.colors.foreground};
  border: 1px solid
    ${({ $isDanger, theme }) =>
      $isDanger ? theme.colors.error : theme.colors.foreground};
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 10px;
  transition: all 0.1s;

  &:hover {
    background: ${({ $isDanger, theme }) =>
      $isDanger ? theme.colors.error : theme.colors.foreground};
    color: ${({ $isDanger, theme }) =>
      $isDanger ? "white" : theme.colors.background};
  }

  @media (max-width: 1024px) {
    width: 100%;
    justify-content: center;
  }
`;

const WaveformViz = styled.div`
  border-left: 1px solid ${({ theme }) => theme.colors.foreground};
  display: flex;
  align-items: center;
  justify-content: center;
  flex-direction: row;
  padding: 10px 20px;
  position: relative;
  gap: 16px;

  @media (max-width: 1024px) {
    display: flex;
    border-left: none;
    border-top: 1px solid ${({ theme }) => theme.colors.foreground};
    padding: 12px 16px;
    order: -1;
  }
`;

const NUM_AUDIO_BARS = 20;

const WaveBars = styled.div`
  display: flex;
  align-items: flex-end;
  gap: 2px;
  height: 40px;
`;

const Bar = styled.div<{ $height: number }>`
  width: 2.5px;
  border-radius: 1.5px;
  background: ${({ theme }) => theme.colors.foreground};
  height: ${({ $height }) => $height}px;
  min-height: 3px;
  transition: height 0.07s ease-out;
`;

const VizLabel = styled.div`
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  opacity: 0.8;
`;

const VoiceToggleButton = styled.button<{ $isActive: boolean }>`
  width: 50px;
  height: 50px;
  border-radius: 50%;
  border: 2px solid
    ${({ $isActive, theme }) =>
      $isActive ? theme.colors.primary : theme.colors.foreground};
  background: ${({ $isActive, theme }) =>
    $isActive ? theme.colors.primary : "transparent"};
  color: ${({ $isActive, theme }) =>
    $isActive ? "white" : theme.colors.foreground};
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.2s;
  font-size: 20px;

  &:hover {
    background: ${({ $isActive, theme }) =>
      $isActive ? theme.colors.primary : "rgba(255, 255, 255, 0.1)"};
  }

  ${({ $isActive }) =>
    $isActive &&
    `
    animation: pulse-glow 1.5s ease-in-out infinite;
  `}
`;

const VoiceStatusContainer = styled.div`
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
`;

const SensitivitySlider = styled.input`
  width: 60px;
  height: 4px;
  -webkit-appearance: none;
  appearance: none;
  background: ${({ theme }) => theme.colors.foreground}30;
  border-radius: 2px;
  outline: none;
  cursor: pointer;

  &::-webkit-slider-thumb {
    -webkit-appearance: none;
    width: 10px;
    height: 10px;
    background: ${({ theme }) => theme.colors.primary};
    border-radius: 50%;
    cursor: pointer;
  }
`;

const SensitivityContainer = styled.div`
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
`;

const SensitivityLabel = styled.div`
  font-size: 8px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  opacity: 0.5;
`;

export default function App() {
  const [activeAgent, setActiveAgent] = useState<string | null>(null);
  const [agents, setAgents] = useState<RobotAgent[]>([]);
  const [isLoadingAgents, setIsLoadingAgents] = useState(true);
  const [agentAvailabilityWarning, setAgentAvailabilityWarning] = useState<
    string | null
  >(null);
  const [brainBackendStatus, setBrainBackendStatus] =
    useState<BrainBackendStatus | null>(null);
  const [agentLoadTimedOut, setAgentLoadTimedOut] = useState(false);
  const [backendWarmupTimedOut, setBackendWarmupTimedOut] = useState(false);
  const isFetchingAgentsRef = useRef(false);
  const agentsLoadStartedAtRef = useRef(Date.now());
  const backendOverrideAppliedRef = useRef(false);
  const useDirectRobot = import.meta.env.VITE_DIRECT_ROBOT === "true";
  const robotWsUrl = import.meta.env.VITE_ROBOT_WS_URL ?? "ws://localhost:9090";
  const simBaseUrl = import.meta.env.VITE_SIM_BASE_URL ?? "http://localhost:8000";
  const [viewMode, setViewMode] = useState<"frontFocus" | "map">("frontFocus");
  const [isDrawerOpen, setIsDrawerOpen] = useState(false);

  // Voice recognition state
  const [isVoiceActive, setIsVoiceActive] = useState(false);
  const [voiceStatus, setVoiceStatus] = useState<string>("Voice Input");
  const [sensitivity, setSensitivity] = useState(0.02);
  const sensitivityRef = useRef(0.02);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const audioChunksRef = useRef<Blob[]>([]);
  const silenceTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const audioContextRef = useRef<AudioContext | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const silenceCheckIntervalRef = useRef<NodeJS.Timeout | null>(null);
  const isRecordingRef = useRef<boolean>(false);
  const hasSpeechRef = useRef<boolean>(false);

  // Audio visualization state
  const [audioLevels, setAudioLevels] = useState<number[]>(() =>
    new Array(NUM_AUDIO_BARS).fill(0),
  );
  const vizFrameRef = useRef<number | null>(null);

  // Keep sensitivity ref in sync
  useEffect(() => {
    sensitivityRef.current = sensitivity;
  }, [sensitivity]);

  useEffect(() => {
    if (backendOverrideAppliedRef.current) {
      return;
    }
    const backendOverride = backendParamsFromUrl();
    if (!backendOverride) {
      return;
    }

    backendOverrideAppliedRef.current = true;
    stripBackendParamsFromUrl();
    agentsLoadStartedAtRef.current = Date.now();
    setAgents([]);
    setActiveAgent(null);
    setIsLoadingAgents(true);
    setAgentAvailabilityWarning(null);
    setBackendWarmupTimedOut(false);
    setAgentLoadTimedOut(false);
    setBrainBackendStatus({
      state: "connecting",
      connected: false,
      message: "Applying brain backend override from URL.",
      uri: backendOverride.websocket_uri ?? null,
      hosted: backendOverride.websocket_uri
        ? backendOverride.websocket_uri.startsWith("wss://agent-v1.innate.bot") ||
          backendOverride.websocket_uri.startsWith("wss://brain.innate.bot")
        : null,
      timestamp: Date.now() / 1000,
    });

    const applyBackendOverride = async () => {
      try {
        if (useDirectRobot) {
          await setBrainBackendConfigDirect(robotWsUrl, backendOverride);
          return;
        }

        const response = await fetch(`${simBaseUrl}/brain_backend_config`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(backendOverride),
        });
        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setIsLoadingAgents(false);
        setAgentLoadTimedOut(true);
        setAgentAvailabilityWarning(
          `Unable to apply brain backend URL override: ${message}`,
        );
      }
    };

    void applyBackendOverride();
  }, [robotWsUrl, simBaseUrl, useDirectRobot]);

  // Real-time audio visualization loop
  useEffect(() => {
    if (!isVoiceActive) {
      setAudioLevels(new Array(NUM_AUDIO_BARS).fill(0));
      return;
    }

    const updateVisualization = () => {
      if (!analyserRef.current) {
        vizFrameRef.current = requestAnimationFrame(updateVisualization);
        return;
      }

      const analyser = analyserRef.current;
      const dataArray = new Uint8Array(analyser.frequencyBinCount);
      analyser.getByteFrequencyData(dataArray);

      // Focus on voice-relevant frequency range (bins 1–50 ≈ 187Hz–9.4kHz at 48kHz)
      // and build a symmetric mirrored visualization (tallest in center)
      const halfBars = NUM_AUDIO_BARS / 2;
      const voiceStart = 1;
      const voiceEnd = 50;
      const voiceRange = voiceEnd - voiceStart;
      const binsPerBar = Math.floor(voiceRange / halfBars);
      const maxBarHeight = 38;

      const halfLevels = new Array(halfBars);
      for (let i = 0; i < halfBars; i++) {
        let sum = 0;
        const start = voiceStart + i * binsPerBar;
        for (let j = start; j < start + binsPerBar; j++) {
          sum += dataArray[j];
        }
        const avg = sum / binsPerBar / 255;
        halfLevels[i] = Math.max(3, avg * maxBarHeight);
      }

      // Mirror: edges are low frequencies, center is mid/high — then
      // sort so the strongest bars sit in the center
      halfLevels.sort((a, b) => a - b);
      const bars = new Array(NUM_AUDIO_BARS);
      for (let i = 0; i < halfBars; i++) {
        bars[i] = halfLevels[i];
        bars[NUM_AUDIO_BARS - 1 - i] = halfLevels[i];
      }

      setAudioLevels(bars);
      vizFrameRef.current = requestAnimationFrame(updateVisualization);
    };

    vizFrameRef.current = requestAnimationFrame(updateVisualization);

    return () => {
      if (vizFrameRef.current !== null) {
        cancelAnimationFrame(vizFrameRef.current);
        vizFrameRef.current = null;
      }
    };
  }, [isVoiceActive]);

  const fetchAgents = useCallback(
    async ({ requestRefresh = false }: { requestRefresh?: boolean } = {}) => {
      if (isFetchingAgentsRef.current) {
        return;
      }

      agentsLoadStartedAtRef.current = Date.now();
      isFetchingAgentsRef.current = true;
      setIsLoadingAgents(true);
      setAgentLoadTimedOut(false);
      setBackendWarmupTimedOut(false);

      try {
        let data: AvailableAgentsResponse;

        if (useDirectRobot) {
          data = await getAvailableAgentsDirect(robotWsUrl);
        } else {
          if (requestRefresh) {
            const refreshResponse = await fetch(
              `${simBaseUrl}/reload_available_agents`,
              { method: "POST" },
            );
            if (!refreshResponse.ok) {
              throw new Error(`Reload failed: HTTP ${refreshResponse.status}`);
            }
            data = (await refreshResponse.json()) as AvailableAgentsResponse;
          } else {
            const response = await fetch(`${simBaseUrl}/available_agents`);
            if (!response.ok) {
              throw new Error(`HTTP ${response.status}`);
            }
            data = (await response.json()) as AvailableAgentsResponse;
          }
        }

        if (data.brain_backend_status) {
          setBrainBackendStatus(data.brain_backend_status);
        }
        const warningDelayElapsed =
          Date.now() - agentsLoadStartedAtRef.current >=
          AGENT_BACKEND_WARNING_DELAY_MS;
        setBackendWarmupTimedOut(warningDelayElapsed);
        const backendWarnsNow = isBackendWarningStatus(
          data.brain_backend_status ?? null,
          warningDelayElapsed,
        );

        if (data.agents && data.agents.length > 0) {
          setAgents(data.agents);
          setAgentAvailabilityWarning(null);
          setAgentLoadTimedOut(false);
          setActiveAgent((previousAgentId) =>
            previousAgentId &&
            data.agents.some((agent) => agent.id === previousAgentId)
              ? previousAgentId
              : null,
          );
        } else {
          setAgents([]);
          setAgentLoadTimedOut(backendWarnsNow);
          if (backendWarnsNow) {
            setAgentAvailabilityWarning(
              data.error ||
                "The brain has not reported any available behavior agents yet.",
            );
          } else {
            setAgentAvailabilityWarning(null);
          }
        }
      } catch (error) {
        const errorMessage =
          error instanceof Error ? error.message : String(error);
        setAgentLoadTimedOut(true);
        setAgentAvailabilityWarning(
          `Unable to load behavior agents: ${errorMessage}`,
        );
        console.error("Error fetching agents:", error);
      } finally {
        setIsLoadingAgents(false);
        isFetchingAgentsRef.current = false;
      }
    },
    [robotWsUrl, simBaseUrl, useDirectRobot],
  );

  useEffect(() => {
    void fetchAgents();
  }, [fetchAgents]);

  useEffect(() => {
    if (useDirectRobot) {
      return;
    }

    let stopped = false;
    const pollBackendStatus = async () => {
      try {
        const response = await fetch(`${simBaseUrl}/stack_metrics`);
        if (!response.ok) {
          return;
        }
        const data = (await response.json()) as StackMetricsResponse;
        if (stopped || !data.brain_backend_status) {
          return;
        }

        setBrainBackendStatus(data.brain_backend_status);
        if (data.brain_backend_status.connected) {
          setBackendWarmupTimedOut(false);
          setAgentLoadTimedOut(false);
        } else if (
          Date.now() - agentsLoadStartedAtRef.current >=
          AGENT_BACKEND_WARNING_DELAY_MS
        ) {
          setBackendWarmupTimedOut(true);
        }
      } catch {
        // The video panel already handles simulator connectivity; keep this poll quiet.
      }
    };

    void pollBackendStatus();
    const intervalId = window.setInterval(
      () => void pollBackendStatus(),
      BACKEND_STATUS_POLL_MS,
    );

    return () => {
      stopped = true;
      window.clearInterval(intervalId);
    };
  }, [simBaseUrl, useDirectRobot]);

  const handleReloadAgents = useCallback(() => {
    void fetchAgents({ requestRefresh: true });
  }, [fetchAgents]);

  // Voice recognition functions
  const SILENCE_DURATION = 1500; // ms of silence before processing speech

  const processAudioBlob = useCallback(async (audioBlob: Blob) => {
    // Don't block - process in background while continuing to listen
    try {
      setVoiceStatus("Sending...");

      const file = new File([audioBlob], "recording.webm", {
        type: "audio/webm",
      });

      // Use dynamic import for Groq to avoid issues
      const { default: Groq } = await import("groq-sdk");
      const groq = new Groq({
        apiKey: import.meta.env.VITE_GROQ_API_KEY || "",
        dangerouslyAllowBrowser: true,
      });

      const transcription = await groq.audio.transcriptions.create({
        file: file,
        model: "whisper-large-v3-turbo",
      });

      if (transcription && transcription.text && transcription.text.trim()) {
        const text = transcription.text.trim();
        console.log("Transcribed:", text);

        // Dispatch custom event for Chat component to send the message
        // This avoids creating a separate WebSocket that would conflict
        window.dispatchEvent(
          new CustomEvent("voice-transcription", { detail: { text } }),
        );

        setVoiceStatus("✓ Sent");
        setTimeout(() => setVoiceStatus("Listening..."), 800);
      } else {
        // No speech detected, go back to listening
        setVoiceStatus("Listening...");
      }
    } catch (error) {
      console.error("Error processing audio:", error);
      setVoiceStatus("Listening...");
    }
  }, []);

  const startRecording = useCallback(() => {
    if (!streamRef.current || isRecordingRef.current) return;

    audioChunksRef.current = [];
    const mediaRecorder = new MediaRecorder(streamRef.current, {
      mimeType: "audio/webm",
    });

    mediaRecorder.ondataavailable = (event) => {
      if (event.data.size > 0) {
        audioChunksRef.current.push(event.data);
      }
    };

    mediaRecorder.onstop = () => {
      if (audioChunksRef.current.length > 0 && hasSpeechRef.current) {
        const audioBlob = new Blob(audioChunksRef.current, {
          type: "audio/webm",
        });
        processAudioBlob(audioBlob);
      }
      hasSpeechRef.current = false;
    };

    mediaRecorder.start(100); // Collect data every 100ms
    mediaRecorderRef.current = mediaRecorder;
    isRecordingRef.current = true;
  }, [processAudioBlob]);

  const stopRecording = useCallback(() => {
    if (
      mediaRecorderRef.current &&
      mediaRecorderRef.current.state !== "inactive"
    ) {
      mediaRecorderRef.current.stop();
    }
    isRecordingRef.current = false;
  }, []);

  const checkAudioLevel = useCallback(() => {
    if (!analyserRef.current) return;

    const dataArray = new Uint8Array(analyserRef.current.frequencyBinCount);
    analyserRef.current.getByteFrequencyData(dataArray);

    // Calculate average volume (0-1 range)
    const average =
      dataArray.reduce((a, b) => a + b, 0) / dataArray.length / 255;

    if (average > sensitivityRef.current) {
      // Sound detected
      hasSpeechRef.current = true;

      if (!isRecordingRef.current) {
        setVoiceStatus("● Recording");
        startRecording();
      }

      // Clear silence timeout - user is still speaking
      if (silenceTimeoutRef.current) {
        clearTimeout(silenceTimeoutRef.current);
        silenceTimeoutRef.current = null;
      }
    } else {
      // Silence detected - wait before finalizing
      if (
        isRecordingRef.current &&
        hasSpeechRef.current &&
        !silenceTimeoutRef.current
      ) {
        silenceTimeoutRef.current = setTimeout(() => {
          stopRecording();
          silenceTimeoutRef.current = null;
        }, SILENCE_DURATION);
      }
    }
  }, [startRecording, stopRecording]);

  const startVoiceRecognition = useCallback(async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;

      // Create audio context and analyser for silence detection
      const AudioContextClass =
        window.AudioContext ||
        (window as unknown as { webkitAudioContext: typeof AudioContext })
          .webkitAudioContext;
      audioContextRef.current = new AudioContextClass();
      analyserRef.current = audioContextRef.current.createAnalyser();
      analyserRef.current.fftSize = 256;

      const source = audioContextRef.current.createMediaStreamSource(stream);
      source.connect(analyserRef.current);

      // Start checking audio levels
      silenceCheckIntervalRef.current = setInterval(checkAudioLevel, 100);

      setIsVoiceActive(true);
      setVoiceStatus("Listening...");
    } catch (error) {
      console.error("Error starting voice recognition:", error);
      setVoiceStatus("Mic Error");
    }
  }, [checkAudioLevel]);

  const stopVoiceRecognition = useCallback(() => {
    // Stop recording if active
    stopRecording();

    // Clear intervals and timeouts
    if (silenceCheckIntervalRef.current) {
      clearInterval(silenceCheckIntervalRef.current);
      silenceCheckIntervalRef.current = null;
    }
    if (silenceTimeoutRef.current) {
      clearTimeout(silenceTimeoutRef.current);
      silenceTimeoutRef.current = null;
    }

    // Stop media stream
    if (streamRef.current) {
      streamRef.current.getTracks().forEach((track) => track.stop());
      streamRef.current = null;
    }

    // Close audio context
    if (audioContextRef.current) {
      audioContextRef.current.close();
      audioContextRef.current = null;
    }

    setIsVoiceActive(false);
    setVoiceStatus("Voice Input");
  }, [stopRecording]);

  const toggleVoiceRecognition = useCallback(() => {
    if (isVoiceActive) {
      stopVoiceRecognition();
    } else {
      startVoiceRecognition();
    }
  }, [isVoiceActive, startVoiceRecognition, stopVoiceRecognition]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      stopVoiceRecognition();
    };
  }, [stopVoiceRecognition]);

  async function handleResetBrain(memory_state?: string) {
    try {
      const isValidMemoryState = typeof memory_state === "string";

      if (useDirectRobot) {
        await resetBrainDirect(
          robotWsUrl,
          isValidMemoryState ? memory_state : undefined,
        );
        if (isValidMemoryState && memory_state) {
          alert(`Brain reset requested with memory state: ${memory_state}!`);
        } else {
          alert("Brain reset requested!");
        }
        return;
      }

      const baseUrl =
        import.meta.env.VITE_SIM_BASE_URL ?? "http://localhost:8000";

      const headers: Record<string, string> = {
        "Content-Type": "application/json",
      };

      const body = isValidMemoryState
        ? JSON.stringify({ memory_state })
        : JSON.stringify({});

      const response = await fetch(`${baseUrl}/reset_brain`, {
        method: "POST",
        headers,
        body,
      });

      if (!response.ok) {
        alert(`Reset Brain failed (HTTP ${response.status}).`);
        return;
      }

      const data = await response.json();
      console.log("Reset brain response:", data);
      if (data.status === "reset_brain_enqueued") {
        if (isValidMemoryState && memory_state) {
          alert(`Brain reset requested with memory state: ${memory_state}!`);
        } else {
          alert("Brain reset requested!");
        }
      }
    } catch (error) {
      console.error("Error resetting brain:", error);
    }
  }

  async function handleResetPosition() {
    try {
      const baseUrl =
        import.meta.env.VITE_SIM_BASE_URL ?? "http://localhost:8000";

      console.log(
        "Requesting reset_position from simulator backend:",
        baseUrl,
        "(direct robot mode:",
        useDirectRobot,
        ")",
      );
      const response = await fetch(`${baseUrl}/reset_position`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({}),
      });

      if (!response.ok) {
        alert(`Reset Position failed (HTTP ${response.status}).`);
        return;
      }

      const data = await response.json();
      console.log("Reset position response:", data);
      if (data.status === "reset_position_enqueued") {
        alert("Position reset requested!");
      }
    } catch (error) {
      console.error("Error resetting position:", error);
      const errorMessage =
        error instanceof Error ? error.message : "Unknown reset error";
      alert(`Reset Position request failed: ${errorMessage}`);
    }
  }

  async function handleSetDirective(directive: string) {
    try {
      if (useDirectRobot) {
        await setDirectiveDirect(robotWsUrl, directive);
        await setBrainActiveDirect(robotWsUrl, true);
        return;
      }

      const baseUrl =
        import.meta.env.VITE_SIM_BASE_URL ?? "http://localhost:8000";

      const headers: Record<string, string> = {
        "Content-Type": "application/json",
      };

      // Set the directive
      const response = await fetch(`${baseUrl}/set_directive`, {
        method: "POST",
        headers,
        body: JSON.stringify({ text: directive }),
      });

      const data = await response.json();
      console.log("Directive response:", data);

      // Also activate the brain when selecting an agent
      const brainResponse = await fetch(`${baseUrl}/set_brain_active`, {
        method: "POST",
        headers,
        body: JSON.stringify({ active: true }),
      });
      const brainData = await brainResponse.json();
      console.log("Brain activation response:", brainData);
    } catch (error) {
      console.error("Error setting directive:", error);
    }
  }

  const backendStatusIsWarning = isBackendWarningStatus(
    brainBackendStatus,
    backendWarmupTimedOut || agentLoadTimedOut,
  );
  const backendIsBrieflyConnected =
    brainBackendStatus?.connected && !isBackendConnectedStable(brainBackendStatus);
  const backendLevel = backendDisplayLevel(brainBackendStatus);
  const backendLabel = backendDisplayLabel(brainBackendStatus);
  const agentWarning = backendStatusIsWarning
    ? {
        title: backendIsBrieflyConnected
          ? "Brain backend connecting"
          : "Brain backend disconnected",
        detail: backendIsBrieflyConnected
          ? "The websocket opened briefly; waiting for the backend to stay connected."
          : brainBackendStatus?.message ||
            `Status: ${formatBackendState(brainBackendStatus?.state)}`,
      }
    : agentAvailabilityWarning
      ? {
          title: "Behavior agents unavailable",
          detail: agentAvailabilityWarning,
        }
      : null;

  return (
    <AppContainer>
      <Header>
        <div style={{ display: "flex", alignItems: "center", height: "100%" }}>
          <HamburgerButton onClick={() => setIsDrawerOpen(!isDrawerOpen)}>
            <svg
              width="24"
              height="24"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth="2"
                d="M4 6h16M4 12h16M4 18h16"
              />
            </svg>
          </HamburgerButton>
          <Logo>INNATE SIM</Logo>
        </div>
        <div></div>
        <StatusBadge $level={backendLevel}>
          <StatusDot />
          Backend: {backendLabel}
        </StatusBadge>
      </Header>

      <Workspace>
        <Sidebar $isOpen={isDrawerOpen}>
          <DrawerHeader>
            <span
              style={{
                fontSize: 14,
                fontWeight: 700,
                textTransform: "uppercase",
                letterSpacing: "0.05em",
                opacity: 0.7,
              }}
            >
              System Configuration
            </span>
            <DrawerCloseButton onClick={() => setIsDrawerOpen(false)}>
              <svg
                width="20"
                height="20"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth="2"
                  d="M6 18L18 6M6 6l12 12"
                />
              </svg>
            </DrawerCloseButton>
          </DrawerHeader>
          <PanelSection>
            <PanelHeader>Unit Identifier</PanelHeader>
            <BigStat>
              <StatValue>MARS</StatValue>
              <StatLabel>Model Type: R7</StatLabel>
            </BigStat>
            <BackendStatusItem $level={backendLevel}>
              <BackendStatusLabel>Brain backend</BackendStatusLabel>
              <BackendStatusValue $level={backendLevel}>
                {backendLabel}
              </BackendStatusValue>
            </BackendStatusItem>
          </PanelSection>

          <PanelSection style={{ flex: 1 }}>
            <PanelHeaderRow>
              <span>Behavior Agents</span>
              <AgentReloadButton
                type="button"
                onClick={handleReloadAgents}
                disabled={isLoadingAgents}
                $isLoading={isLoadingAgents}
                aria-label="Reload behavior agents"
                title="Reload behavior agents"
              >
                <IoRefresh size={15} />
              </AgentReloadButton>
            </PanelHeaderRow>
            <div>
              {agentWarning && (
                <AgentNotice role="alert">
                  <AgentNoticeTitle>{agentWarning.title}</AgentNoticeTitle>
                  <AgentNoticeDetail>{agentWarning.detail}</AgentNoticeDetail>
                </AgentNotice>
              )}
              {agents.length === 0 ? (
                isLoadingAgents ? (
                  <AgentStatusItem>Loading agents...</AgentStatusItem>
                ) : agentWarning ? null : (
                  <AgentStatusItem>Waiting for robot connection</AgentStatusItem>
                )
              ) : (
                agents.map((agent) => (
                  <AgentItem
                    key={agent.id}
                    $isActive={agent.id === activeAgent}
                    onClick={() => {
                      setActiveAgent(agent.id);
                      handleSetDirective(agent.id);
                    }}
                  >
                    <AgentName>{agent.display_name}</AgentName>
                    <AgentCheck $isActive={agent.id === activeAgent}>
                      {agent.id === activeAgent && "✓"}
                    </AgentCheck>
                  </AgentItem>
                ))
              )}
            </div>
          </PanelSection>
          <div style={{ padding: 16, marginTop: "auto" }}>
            <ActionButton
              $isDanger
              onClick={() => {
                handleResetBrain();
                setIsDrawerOpen(false);
              }}
              style={{ width: "100%", justifyContent: "center" }}
            >
              Reset Brain
            </ActionButton>
          </div>
        </Sidebar>

        <MainContent>
          <ImageDisplay
            viewMode={viewMode}
            setViewMode={setViewMode}
            onResetRobot={handleResetBrain}
            onSetDirective={handleSetDirective}
          />
        </MainContent>

        <ChatColumn>
          <PanelHeader>Interaction Log</PanelHeader>
          <Chat />
        </ChatColumn>
      </Workspace>

      <Footer>
        <ControlPanel>
          <div style={{ flex: 1 }}></div>
          <ActionButton $isDanger onClick={() => void handleResetPosition()}>
            Reset Position
          </ActionButton>
        </ControlPanel>

        <WaveformViz>
          <VoiceToggleButton
            $isActive={isVoiceActive}
            onClick={toggleVoiceRecognition}
            title={isVoiceActive ? "Stop listening" : "Start listening"}
          >
            {isVoiceActive ? <IoMic size={24} /> : <IoMicOff size={24} />}
          </VoiceToggleButton>
          <VoiceStatusContainer>
            <WaveBars style={{ opacity: isVoiceActive ? 1 : 0.3 }}>
              {audioLevels.map((level, i) => (
                <Bar key={i} $height={level} />
              ))}
            </WaveBars>
            <VizLabel>{voiceStatus}</VizLabel>
          </VoiceStatusContainer>
          <SensitivityContainer>
            <SensitivitySlider
              type="range"
              min="0.005"
              max="0.08"
              step="0.005"
              value={sensitivity}
              onChange={(e) => setSensitivity(parseFloat(e.target.value))}
              title={`Mic sensitivity`}
            />
            <SensitivityLabel>Sensitivity</SensitivityLabel>
          </SensitivityContainer>
        </WaveformViz>
      </Footer>

      <DrawerOverlay
        $isOpen={isDrawerOpen}
        onClick={() => setIsDrawerOpen(false)}
      />
    </AppContainer>
  );
}
