import styled from "styled-components";
import { isMobile } from "react-device-detect";
import { useState, useEffect, useRef, useCallback } from "react";
import {
  PreviewContainer,
  MainImage,
  SecondaryImage,
  MainVideo,
  SecondaryVideo,
} from "../styles/StyledImages";
import { useRobotWebRTC } from "../hooks/useRobotWebRTC";

type ViewMode = "sideBySide" | "frontFocus" | "chaseFocus";

type ImageDisplayProps = {
  viewMode: ViewMode;
  setViewMode: React.Dispatch<React.SetStateAction<ViewMode>>;
  onResetRobot?: () => void;
  onSetDirective: (directive: string) => void;
};

// Feed Toolbar with view tabs
const FeedToolbar = styled.div`
  display: flex;
  border-bottom: 1px solid ${({ theme }) => theme.colors.foreground};
  background: ${({ theme }) => theme.colors.background};
`;

const ViewBtn = styled.div<{ $isActive: boolean }>`
  flex: 1;
  padding: 12px;
  text-align: center;
  font-size: 11px;
  text-transform: uppercase;
  cursor: pointer;
  border-right: 1px solid ${({ theme }) => theme.colors.foreground};
  background: ${({ $isActive, theme }) =>
    $isActive ? theme.colors.foreground : theme.colors.background};
  transition: all 0.2s;
  color: ${({ $isActive, theme }) =>
    $isActive ? theme.colors.background : theme.colors.foreground};

  &:last-child {
    border-right: none;
  }

  &:hover {
    background: ${({ $isActive, theme }) =>
      $isActive ? theme.colors.foreground : "rgba(255, 255, 255, 0.1)"};
  }
`;

// Camera Viewport - fills available space in the 4:3 container
const CameraViewport = styled.div`
  flex: 1;
  position: relative;
  background: #111;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  width: 100%;
`;

// Simulation Grid Background
const SimGrid = styled.div`
  position: absolute;
  width: 200%;
  height: 200%;
  background-image:
    linear-gradient(
      ${({ theme }) => theme.colors.foreground} 1px,
      transparent 1px
    ),
    linear-gradient(
      90deg,
      ${({ theme }) => theme.colors.foreground} 1px,
      transparent 1px
    );
  background-size: 50px 50px;
  opacity: 0.1;
  transform: perspective(500px) rotateX(60deg) translateY(-100px)
    translateZ(-100px);
  animation: gridMove 20s linear infinite;
`;

// Overlay UI
const OverlayUI = styled.div`
  position: absolute;
  top: 20px;
  left: 20px;
  right: 20px;
  bottom: 20px;
  pointer-events: none;
  border: 1px solid rgba(255, 255, 255, 0.1);
`;

const CamLabel = styled.div`
  position: absolute;
  top: 0;
  left: 0;
  background: ${({ theme }) => theme.colors.foreground};
  color: ${({ theme }) => theme.colors.background};
  padding: 4px 8px;
  font-size: 10px;
  text-transform: uppercase;
`;

const Crosshair = styled.div`
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  width: 40px;
  height: 40px;
`;

const CrosshairH = styled.div`
  position: absolute;
  top: 19px;
  left: 0;
  width: 40px;
  height: 2px;
  background: ${({ theme }) => theme.colors.primary};
`;

const CrosshairV = styled.div`
  position: absolute;
  left: 19px;
  top: 0;
  height: 40px;
  width: 2px;
  background: ${({ theme }) => theme.colors.primary};
`;

const Coords = styled.div`
  position: absolute;
  bottom: 0;
  left: 0;
  background: ${({ theme }) => theme.colors.foreground};
  color: ${({ theme }) => theme.colors.background};
  padding: 4px 8px;
  font-size: 10px;
`;

const ModalOverlay = styled.div`
  position: fixed;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  background: rgba(0, 0, 0, 0.6);
  display: flex;
  justify-content: center;
  align-items: center;
  z-index: 1000;
`;

const ModalContent = styled.div`
  background: #111;
  border: 1px solid ${({ theme }) => theme.colors.foreground};
  padding: 24px;
  width: 400px;
  max-width: 90%;
`;

const ModalTitle = styled.h3`
  color: white;
  margin: 0 0 16px 0;
  font-size: 16px;
  font-family: ${({ theme }) => theme.fonts.display};
  text-transform: uppercase;
`;

const ModalInput = styled.input`
  width: 100%;
  padding: 10px 12px;
  border: 1px solid ${({ theme }) => theme.colors.foreground};
  background: ${({ theme }) => theme.colors.background};
  color: ${({ theme }) => theme.colors.foreground};
  font-size: 14px;
  font-family: ${({ theme }) => theme.fonts.mono};
  box-sizing: border-box;

  &:focus {
    outline: none;
    border-color: ${({ theme }) => theme.colors.primary};
  }

  &::placeholder {
    color: rgba(255, 255, 255, 0.5);
  }
`;

const ModalButtons = styled.div`
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 16px;
`;

const ModalButton = styled.button<{ $primary?: boolean }>`
  padding: 8px 16px;
  font-size: 12px;
  text-transform: uppercase;
  cursor: pointer;
  transition: all 0.2s;
  font-family: ${({ theme }) => theme.fonts.mono};

  ${({ $primary, theme }) =>
    $primary
      ? `
    background: ${theme.colors.primary};
    color: white;
    border: 1px solid ${theme.colors.primary};
    &:hover {
      background: ${theme.colors.primaryHover};
    }
  `
      : `
    background: transparent;
    color: ${theme.colors.foreground};
    border: 1px solid ${theme.colors.foreground};
    &:hover {
      background: ${theme.colors.foreground};
      color: ${theme.colors.background};
    }
  `}
`;

// Loading indicator styled component
const LoadingContainer = styled.div`
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;
  background-color: rgba(30, 41, 59, 0.9);
  z-index: 200;
  border-radius: 8px;
`;

const Spinner = styled.div`
  width: 50px;
  height: 50px;
  border: 5px solid rgba(255, 255, 255, 0.3);
  border-radius: 50%;
  border-top-color: #4f46e5;
  animation: spin 1s ease-in-out infinite;
  margin-bottom: 16px;

  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }
`;

const LoadingText = styled.p`
  color: white;
  font-size: 16px;
  font-weight: 500;
  text-align: center;
  max-width: 80%;
  margin: 0 auto;
`;

const ErrorText = styled(LoadingText)`
  color: #f87171;
  margin-bottom: 8px;
`;

const RetryButton = styled.button`
  background-color: #4f46e5;
  color: white;
  border: none;
  border-radius: 4px;
  padding: 8px 16px;
  font-size: 14px;
  cursor: pointer;
  margin-top: 16px;
  transition: background-color 0.2s;

  &:hover {
    background-color: #4338ca;
  }

  &:disabled {
    background-color: #6b7280;
    cursor: not-allowed;
  }
`;

// Interface for the video feeds ready response
interface SimulationReadyResponse {
  ready: boolean;
  message: string;
}

export function ImageDisplay({
  viewMode,
  setViewMode,
  onSetDirective,
}: ImageDisplayProps) {
  const [backendShowLoading, setBackendShowLoading] = useState(true);
  const [backendConnectionFailed, setBackendConnectionFailed] = useState(false);
  const [backendErrorMessage, setBackendErrorMessage] = useState(
    "Simulation not running",
  );
  const [isCheckingBackend, setIsCheckingBackend] = useState(false);
  const [showDirectiveModal, setShowDirectiveModal] = useState(false);
  const [directiveText, setDirectiveText] = useState("");

  const useDirectRobot = import.meta.env.VITE_DIRECT_ROBOT === "true";
  const robotWsUrl = import.meta.env.VITE_ROBOT_WS_URL ?? "ws://localhost:9090";
  const {
    mainStream,
    secondaryStream,
    hasMedia,
    isConnecting: isWebRTCConnecting,
    error: webRTCError,
    reconnect: reconnectWebRTC,
  } = useRobotWebRTC({
    enabled: useDirectRobot,
    wsUrl: robotWsUrl,
    source: "live",
  });

  const mainVideoRef = useRef<HTMLVideoElement | null>(null);
  const secondaryVideoRef = useRef<HTMLVideoElement | null>(null);

  const connectionFailed = useDirectRobot
    ? Boolean(webRTCError)
    : backendConnectionFailed;
  const directMainStream =
    viewMode === "chaseFocus" ? (secondaryStream ?? mainStream) : mainStream;
  const directSecondaryStream =
    viewMode === "chaseFocus" ? mainStream : secondaryStream;
  const showLoading = useDirectRobot
    ? !hasMedia && !webRTCError
    : backendShowLoading;
  const errorMessage = useDirectRobot
    ? (webRTCError ?? "Failed to connect to robot WebRTC stream.")
    : backendErrorMessage;

  const baseUrl = import.meta.env.VITE_SIM_BASE_URL ?? "http://localhost:8000";

  // Set up the sources for the main and secondary feeds based on view mode
  let mainSrc = baseUrl + "/video_feed";
  let subSrc = baseUrl + "/video_feed_chase";

  if (viewMode === "chaseFocus") {
    mainSrc = baseUrl + "/video_feed_chase";
    subSrc = baseUrl + "/video_feed";
  } else if (viewMode === "frontFocus") {
    mainSrc = baseUrl + "/video_feed";
    subSrc = baseUrl + "/video_feed_chase";
  } else if (viewMode === "sideBySide") {
    // In side-by-side mode, we keep the original sources
    mainSrc = baseUrl + "/video_feed";
    subSrc = baseUrl + "/video_feed_chase";
  }

  // Function to check if the simulation is running
  const checkSimulationReady = useCallback(async () => {
    if (useDirectRobot) {
      return;
    }

    setIsCheckingBackend(true);

    try {
      const response = await fetch(`${baseUrl}/video_feeds_ready`);

      if (response.ok) {
        const data: SimulationReadyResponse = await response.json();

        if (data.ready) {
          // Simulation is running, hide loading screen
          setBackendShowLoading(false);
          setBackendConnectionFailed(false);
        } else {
          // Simulation is not running, show error message
          setBackendConnectionFailed(true);
          setBackendErrorMessage(data.message);
        }
      } else {
        // API call failed
        setBackendConnectionFailed(true);
        setBackendErrorMessage("Failed to connect to the server");
      }
    } catch (error) {
      console.error("Error checking simulation status:", error);
      setBackendConnectionFailed(true);
      setBackendErrorMessage("Error connecting to the server");
    } finally {
      setIsCheckingBackend(false);
    }
  }, [baseUrl, useDirectRobot]);

  // Check simulation when component mounts or viewMode changes
  useEffect(() => {
    if (useDirectRobot) {
      return;
    }

    checkSimulationReady();

    // Set up polling to periodically check if simulation becomes available
    const intervalId = setInterval(() => {
      if (backendConnectionFailed) {
        checkSimulationReady();
      }
    }, 5000); // Check every 5 seconds if in failed state

    return () => {
      clearInterval(intervalId);
    };
  }, [viewMode, useDirectRobot, backendConnectionFailed, checkSimulationReady]);

  useEffect(() => {
    if (!useDirectRobot || !mainVideoRef.current) {
      return;
    }

    const video = mainVideoRef.current;
    video.srcObject = directMainStream;
    video.autoplay = true;
    video.muted = true;
    video.playsInline = true;
    if (directMainStream) {
      void video.play().catch(() => {});
    }
  }, [useDirectRobot, directMainStream]);

  useEffect(() => {
    if (!useDirectRobot || !secondaryVideoRef.current) {
      return;
    }

    const video = secondaryVideoRef.current;
    video.srcObject = directSecondaryStream;
    video.autoplay = true;
    video.muted = true;
    video.playsInline = true;
    if (directSecondaryStream) {
      void video.play().catch(() => {});
    }
  }, [useDirectRobot, directSecondaryStream]);

  // Only show all modes on desktop. On mobile, remove "sideBySide".
  const desktopModes: ViewMode[] = ["sideBySide", "frontFocus", "chaseFocus"];
  const mobileModes: ViewMode[] = ["frontFocus", "chaseFocus"];
  const modes: ViewMode[] = isMobile ? mobileModes : desktopModes;

  // Ensure that if a user is on mobile and currently has "sideBySide",
  // we switch them to a mobile-supported mode (e.g. "frontFocus").
  if (isMobile && viewMode === "sideBySide") {
    setViewMode("frontFocus");
  }

  const labels: Record<ViewMode, string> = {
    sideBySide: "Side By Side",
    frontFocus: "Front Focus",
    chaseFocus: "Chase Focus",
  };

  // Handle view mode change
  const handleViewModeChange = (newMode: ViewMode) => {
    if (newMode !== viewMode) {
      setViewMode(newMode);
    }
  };

  return (
    <PreviewContainer>
      {/* View Mode Toolbar */}
      <FeedToolbar>
        {modes.map((mode) => (
          <ViewBtn
            key={mode}
            $isActive={viewMode === mode}
            onClick={() => handleViewModeChange(mode)}
          >
            {labels[mode]}
          </ViewBtn>
        ))}
      </FeedToolbar>

      {/* Camera Viewport */}
      <CameraViewport>
        {/* Background Grid */}
        <SimGrid />

        {useDirectRobot ? (
          <>
            <MainVideo
              ref={mainVideoRef}
              $viewMode={viewMode}
              muted
              autoPlay
              playsInline
            />
            <SecondaryVideo
              ref={secondaryVideoRef}
              $viewMode={viewMode}
              muted
              autoPlay
              playsInline
            />
          </>
        ) : (
          <>
            <MainImage $viewMode={viewMode} src={mainSrc} alt="Main Camera" />
            <SecondaryImage
              $viewMode={viewMode}
              src={subSrc}
              alt="Secondary Camera"
            />
          </>
        )}

        {/* Loading indicator */}
        {showLoading && (
          <LoadingContainer>
            {connectionFailed ? (
              <>
                <ErrorText>{errorMessage}</ErrorText>
                <LoadingText>
                  {useDirectRobot
                    ? "Please check robot WebRTC availability and try again."
                    : "Please check if the simulation is running and try again."}
                </LoadingText>
                <RetryButton
                  onClick={
                    useDirectRobot ? reconnectWebRTC : checkSimulationReady
                  }
                  disabled={useDirectRobot ? false : isCheckingBackend}
                >
                  {useDirectRobot
                    ? isWebRTCConnecting
                      ? "Reconnecting..."
                      : "Retry Connection"
                    : isCheckingBackend
                      ? "Checking..."
                      : "Retry Connection"}
                </RetryButton>
              </>
            ) : (
              <>
                <Spinner />
                <LoadingText>Loading camera feed...</LoadingText>
              </>
            )}
          </LoadingContainer>
        )}

        {/* Overlay UI */}
        {!showLoading && (
          <OverlayUI>
            <CamLabel>LIVE FEED</CamLabel>
            <Crosshair>
              <CrosshairH />
              <CrosshairV />
            </Crosshair>
            <Coords>X: 45.2 Y: 12.0 Z: 0.4</Coords>
          </OverlayUI>
        )}
      </CameraViewport>

      {/* Directive Modal */}
      {showDirectiveModal && (
        <ModalOverlay onClick={() => setShowDirectiveModal(false)}>
          <ModalContent onClick={(e) => e.stopPropagation()}>
            <ModalTitle>Set Directive</ModalTitle>
            <ModalInput
              type="text"
              placeholder="Enter directive..."
              value={directiveText}
              onChange={(e) => setDirectiveText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && directiveText.trim()) {
                  onSetDirective(directiveText.trim());
                  setDirectiveText("");
                  setShowDirectiveModal(false);
                }
              }}
              autoFocus
            />
            <ModalButtons>
              <ModalButton onClick={() => setShowDirectiveModal(false)}>
                Cancel
              </ModalButton>
              <ModalButton
                $primary
                onClick={() => {
                  if (directiveText.trim()) {
                    onSetDirective(directiveText.trim());
                    setDirectiveText("");
                    setShowDirectiveModal(false);
                  }
                }}
              >
                Send
              </ModalButton>
            </ModalButtons>
          </ModalContent>
        </ModalOverlay>
      )}
    </PreviewContainer>
  );
}
