import styled from "styled-components";
import { isMobile } from "react-device-detect";
import { MdRefresh } from "react-icons/md";
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

const ResetButton = styled.button`
  position: absolute;
  top: 10px;
  left: 10px;
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

export function ImageDisplay({
  viewMode,
  setViewMode,
  onResetRobot,
}: ImageDisplayProps) {
  // Grab IP from environment, use a fallback if missing
  const baseUrl = import.meta.env.VITE_SIM_BASE_URL ?? "http://localhost:8000";

  let mainSrc = baseUrl + "/video_feed";
  let subSrc = baseUrl + "/video_feed_chase";

  if (viewMode === "chaseFocus") {
    mainSrc = baseUrl + "/video_feed_chase";
    subSrc = baseUrl + "/video_feed";
  } else if (viewMode === "frontFocus") {
    mainSrc = baseUrl + "/video_feed";
    subSrc = baseUrl + "/video_feed_chase";
  }

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

  return (
    <PreviewContainer>
      <MainImage $viewMode={viewMode} src={mainSrc} alt="Main Camera" />
      <SecondaryImage $viewMode={viewMode} src={subSrc} alt="Sub Camera" />

      <ResetButton onClick={onResetRobot}>
        <MdRefresh size={16} /> Reset Robot
      </ResetButton>

      <ToggleWrapper>
        <Indicator index={currentIndex} $maxIndex={modes.length} />
        <ButtonRow>
          {modes.map((mode) => (
            <ModeButton
              key={mode}
              $active={viewMode === mode}
              onClick={() => setViewMode(mode)}
            >
              {labels[mode]}
            </ModeButton>
          ))}
        </ButtonRow>
      </ToggleWrapper>
    </PreviewContainer>
  );
}
