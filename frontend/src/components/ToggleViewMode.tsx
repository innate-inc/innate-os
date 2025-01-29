/**
 * Segmented control to switch between "sideBySide", "frontFocus", or "chaseFocus"
 */
import React from "react";
import styled from "styled-components";

type ViewMode = "sideBySide" | "frontFocus" | "chaseFocus";

type Props = {
  viewMode: ViewMode;
  setViewMode: React.Dispatch<React.SetStateAction<ViewMode>>;
};

const ToggleWrapper = styled.div`
  margin-top: 20px;
  display: inline-block;
  position: relative;
  width: 300px;
  background: #e5e5ea;
  border-radius: 25px;
  overflow: hidden;
  box-shadow: inset 0 0 1px rgba(0, 0, 0, 0.25);
`;

const Indicator = styled.div<{ index: number }>`
  position: absolute;
  top: 0;
  left: 0;
  width: calc(100% / 3);
  height: 100%;
  transform: translateX(${(props) => props.index * 100}%);
  background: #ffffff;
  border-radius: 25px;
  box-shadow: 0 0 5px rgba(0, 0, 0, 0.2);
  transition: transform 0.3s ease;
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
  font-size: 14px;
  padding: 8px 0;
  cursor: pointer;
  border-radius: 25px;

  &:hover {
    opacity: 0.8;
  }

  &:focus {
    outline: none;
  }
`;

export function ToggleViewMode({ viewMode, setViewMode }: Props) {
  const modes: ViewMode[] = ["sideBySide", "frontFocus", "chaseFocus"];
  const labels: Record<ViewMode, string> = {
    sideBySide: "Side By Side",
    frontFocus: "Front Focus",
    chaseFocus: "Chase Focus",
  };
  const currentIndex = modes.indexOf(viewMode);

  return (
    <ToggleWrapper>
      <Indicator index={currentIndex} />
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
