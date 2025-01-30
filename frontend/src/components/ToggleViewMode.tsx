/**
 * Segmented control to switch between "sideBySide", "frontFocus", or "chaseFocus"
 */
import React from "react";
import styled from "styled-components";
import { isMobile } from "react-device-detect";

type ViewMode = "sideBySide" | "frontFocus" | "chaseFocus";

type Props = {
  viewMode: ViewMode;
  setViewMode: React.Dispatch<React.SetStateAction<ViewMode>>;
};

const ToggleWrapper = styled.div`
  margin-top: 20px;
  display: inline-block;
  position: relative;
  width: calc(min(380px, 100%));
  background: #e5e5ea;
  border: 1px solid #c7c7cc;
  border-radius: 25px;
  overflow: hidden;

  @media (prefers-color-scheme: dark) {
    background: #333;
    border-color: #444;
  }
`;

const Indicator = styled.div<{ index: number; maxIndex: number }>`
  position: absolute;
  top: 0;
  left: 0;
  width: calc(100% / ${({ maxIndex }) => maxIndex});
  height: 100%;
  transform: translateX(${(props) => props.index * 100}%);
  background: #ffffff;
  border-radius: 25px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.2);
  transition: transform 0.3s ease;

  @media (prefers-color-scheme: dark) {
    background: #666;
  }
`;

const ButtonRow = styled.div`
  display: flex;
  width: 100%;
`;

const ToggleButton = styled.button<{ active?: boolean }>`
  flex: 1;
  position: relative;
  z-index: 1;
  background: transparent;
  border: none;
  color: ${(props) => (props.active ? "#007aff" : "#8e8e93")};
  font-size: 16px;
  padding: 6px 0;
  cursor: pointer;
  border-radius: 25px;

  &:hover {
    opacity: 0.8;
  }

  &:focus {
    outline: none;
  }

  @media (prefers-color-scheme: dark) {
    color: ${(props) => (props.active ? "#4c9aff" : "#bbb")};
  }
`;

export function ToggleViewMode({ viewMode, setViewMode }: Props) {
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
    <ToggleWrapper>
      <Indicator index={currentIndex} maxIndex={modes.length} />
      <ButtonRow>
        {modes.map((mode) => (
          <ToggleButton
            key={mode}
            active={viewMode === mode}
            onClick={() => setViewMode(mode)}
          >
            {labels[mode]}
          </ToggleButton>
        ))}
      </ButtonRow>
    </ToggleWrapper>
  );
}
