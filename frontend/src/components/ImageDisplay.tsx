import styled from "styled-components";
import { isMobile } from "react-device-detect";
import { MdRefresh, MdSend } from "react-icons/md";
import { useState, useEffect } from "react";
import {
  PreviewContainer,
  MainImage,
  SecondaryImage,
} from "../styles/StyledImages";

type ViewMode = "sideBySide" | "frontFocus" | "chaseFocus";

type ImageDisplayProps = {
  viewMode: ViewMode;
  setViewMode: React.Dispatch<React.SetStateAction<ViewMode>>;
  onResetRobot: () => void;
  onSetDirective: (directive: string) => void;
};

// Styled components for the slider, adapted from ToggleViewMode
const ToggleWrapper = styled.div`
  position: absolute;
  top: 10px;
  right: 10px;
  z-index: 100;
  width: 350px;
  background: rgba(0, 0, 0, 0.7);
  border: 1px solid rgba(255, 255, 255, 0.2);
  border-radius: 25px;
  overflow: hidden;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.3);
  backdrop-filter: blur(5px);
  -webkit-backdrop-filter: blur(5px);
`;

const ControlButton = styled.button`
  z-index: 100;
  background: rgba(0, 0, 0, 0.7);
  border: 1px solid rgba(255, 255, 255, 0.2);
  border-radius: 4px;
  padding: 6px 10px;
  color: white;
  font-size: 14px;
  cursor: pointer;
  transition: all 0.2s ease;
  display: flex;
  align-items: center;
  gap: 5px;

  &:hover {
    background: rgba(0, 0, 0, 0.8);
  }

  &:focus {
    outline: none;
  }
`;

const ButtonGroup = styled.div`
  position: absolute;
  top: 10px;
  left: 10px;
  display: flex;
  gap: 8px;
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
  background: #1e293b;
  border-radius: 8px;
  padding: 24px;
  width: 400px;
  max-width: 90%;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
`;

const ModalTitle = styled.h3`
  color: white;
  margin: 0 0 16px 0;
  font-size: 18px;
`;

const ModalInput = styled.input`
  width: 100%;
  padding: 10px 12px;
  border: 1px solid rgba(255, 255, 255, 0.2);
  border-radius: 4px;
  background: rgba(0, 0, 0, 0.3);
  color: white;
  font-size: 14px;
  box-sizing: border-box;

  &:focus {
    outline: none;
    border-color: #4f46e5;
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
  border-radius: 4px;
  font-size: 14px;
  cursor: pointer;
  transition: background-color 0.2s;
  border: none;

  ${({ $primary }) =>
    $primary
      ? `
    background: #4f46e5;
    color: white;
    &:hover {
      background: #4338ca;
    }
  `
      : `
    background: rgba(255, 255, 255, 0.1);
    color: white;
    &:hover {
      background: rgba(255, 255, 255, 0.2);
    }
  `}
`;

const Indicator = styled.div<{ index: number; $maxIndex: number }>`
  position: absolute;
  top: 2px;
  left: 2px;
  width: calc((100% - 4px) / ${({ $maxIndex }) => $maxIndex});
  height: calc(100% - 4px);
  transform: translateX(${(props) => props.index * 100}%);
  background: rgba(79, 70, 229, 0.6);
  border-radius: 23px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.2);
  transition: transform 0.3s ease;
`;

const ButtonRow = styled.div`
  display: flex;
  width: 100%;
`;

const ModeButton = styled.button<{ $active?: boolean }>`
  flex: 1;
  position: relative;
  z-index: 1;
  background: transparent;
  border: none;
  color: ${(props) => (props.$active ? "#ffffff" : "rgba(255, 255, 255, 0.7)")};
  font-size: 14px;
  padding: 6px 0;
  cursor: pointer;
  border-radius: 25px;

  &:hover {
    opacity: 0.8;
  }

  &:focus {
    outline: none;
  }
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
  onResetRobot,
  onSetDirective,
}: ImageDisplayProps) {
  // State to track if we should show the loading screen
  const [showLoading, setShowLoading] = useState(true);
  // State to track if we've failed to connect to the simulation
  const [connectionFailed, setConnectionFailed] = useState(false);
  // State to store the error message
  const [errorMessage, setErrorMessage] = useState("Simulation not running");
  // State to track if we're checking the simulation
  const [isChecking, setIsChecking] = useState(false);
  const [showDirectiveModal, setShowDirectiveModal] = useState(false);
  const [directiveText, setDirectiveText] = useState("");

  // Grab IP from environment, use a fallback if missing
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
  const checkSimulationReady = async () => {
    setIsChecking(true);

    try {
      const response = await fetch(`${baseUrl}/video_feeds_ready`);

      if (response.ok) {
        const data: SimulationReadyResponse = await response.json();

        if (data.ready) {
          // Simulation is running, hide loading screen
          setShowLoading(false);
          setConnectionFailed(false);
        } else {
          // Simulation is not running, show error message
          setConnectionFailed(true);
          setErrorMessage(data.message);
        }
      } else {
        // API call failed
        setConnectionFailed(true);
        setErrorMessage("Failed to connect to the server");
      }
    } catch (error) {
      console.error("Error checking simulation status:", error);
      setConnectionFailed(true);
      setErrorMessage("Error connecting to the server");
    } finally {
      setIsChecking(false);
    }
  };

  // Check simulation when component mounts or viewMode changes
  useEffect(() => {
    checkSimulationReady();

    // Set up polling to periodically check if simulation becomes available
    const intervalId = setInterval(() => {
      if (connectionFailed) {
        checkSimulationReady();
      }
    }, 5000); // Check every 5 seconds if in failed state

    return () => {
      clearInterval(intervalId);
    };
  }, [viewMode]);

  // Only show all modes on desktop. On mobile, remove "sideBySide".
  const desktopModes: ViewMode[] = ["sideBySide", "frontFocus", "chaseFocus"];
  const mobileModes: ViewMode[] = ["frontFocus", "chaseFocus"];
  const modes: ViewMode[] = isMobile ? mobileModes : desktopModes;

  // Convert the current mode into an index, default to 0 if not found
  const currentIndex =
    modes.indexOf(viewMode) >= 0 ? modes.indexOf(viewMode) : 0;

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
      {/* Main feed */}
      <MainImage $viewMode={viewMode} src={mainSrc} alt="Main Camera" />

      {/* Secondary feed */}
      <SecondaryImage
        $viewMode={viewMode}
        src={subSrc}
        alt="Secondary Camera"
      />

      {/* Loading indicator */}
      {showLoading && (
        <LoadingContainer>
          {connectionFailed ? (
            <>
              <ErrorText>{errorMessage}</ErrorText>
              <LoadingText>
                Please check if the simulation is running and try again.
              </LoadingText>
              <RetryButton onClick={checkSimulationReady} disabled={isChecking}>
                {isChecking ? "Checking..." : "Retry Connection"}
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

      {/* Only show controls when not in loading state */}
      {!showLoading && (
        <>
          <ButtonGroup>
            <ControlButton onClick={() => onResetRobot()}>
              <MdRefresh size={16} /> Reset Robot
            </ControlButton>
            <ControlButton onClick={() => setShowDirectiveModal(true)}>
              <MdSend size={16} /> Set Directive
            </ControlButton>
          </ButtonGroup>

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

          <ToggleWrapper>
            <Indicator index={currentIndex} $maxIndex={modes.length} />
            <ButtonRow>
              {modes.map((mode) => (
                <ModeButton
                  key={mode}
                  $active={viewMode === mode}
                  onClick={() => handleViewModeChange(mode)}
                >
                  {labels[mode]}
                </ModeButton>
              ))}
            </ButtonRow>
          </ToggleWrapper>
        </>
      )}
    </PreviewContainer>
  );
}
