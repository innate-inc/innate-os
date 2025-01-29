import React, { useState } from "react";
import styled from "styled-components";
import "./App.css";

const Container = styled.div`
  text-align: center;
`;

const PreviewContainer = styled.div<{ viewMode: string }>`
  position: relative;
  width: 100%;
  margin: 0 auto;
  display: flex;
  justify-content: center;
  align-items: center;
  /* The default "side-by-side" layout */
  ${({ viewMode }) =>
    viewMode === "sideBySide" &&
    `
    flex-direction: row;
  `}
  /* Front camera large, chase camera bottom-right */
  ${({ viewMode }) =>
    viewMode === "frontFocus" &&
    `
    flex-direction: column;
    align-items: flex-start;
    position: relative;
  `}
  /* Chase camera large, front camera bottom-right */
  ${({ viewMode }) =>
    viewMode === "chaseFocus" &&
    `
    flex-direction: column;
    align-items: flex-start;
    position: relative;
  `}
`;

const MainImage = styled.img<{ viewMode: string }>`
  border: 1px solid #ccc;
  ${({ viewMode }) =>
    viewMode === "sideBySide" &&
    `
    width: 640px;
  `}
  ${({ viewMode }) =>
    viewMode === "frontFocus" &&
    `
    width: 640px;
  `}
  ${({ viewMode }) =>
    viewMode === "chaseFocus" &&
    `
    width: 640px;
  `}
`;

const SecondaryImage = styled.img<{ viewMode: string }>`
  border: 1px solid #ccc;

  ${({ viewMode }) =>
    viewMode === "sideBySide" &&
    `
    width: 640px;
  `}

  /* For the "focus" modes, we're making the secondary image smaller 
     and placing it at bottom/right corner */
  ${({ viewMode }) =>
    (viewMode === "frontFocus" || viewMode === "chaseFocus") &&
    `
    position: absolute;
    width: 240px;
    bottom: 10px;
    right: 10px;
  `}
`;

/* 
   A wrapper for the entire segmented control (the "iOS-style" toggle) 
*/
const ToggleWrapper = styled.div`
  margin-top: 20px;
  display: inline-block;
  position: relative;
  width: 300px; /* Adjust as desired */
  background: #e5e5ea; /* Light gray, similar to iOS segmented background */
  border-radius: 25px;
  overflow: hidden;
  box-shadow: inset 0 0 1px rgba(0, 0, 0, 0.25);
  /* subtle inner shadow to mimic iOS segmented style */
`;

/* 
   This sliding indicator sits behind the selected option 
   and slides left/right with transition. 
*/
const Indicator = styled.div<{ index: number }>`
  position: absolute;
  top: 0;
  left: 0;
  width: calc(100% / 3); /* because we have 3 segments */
  height: 100%;
  transform: translateX(${(props) => props.index * 100}%);
  background: #ffffff;
  border-radius: 25px;
  box-shadow: 0 0 5px rgba(0, 0, 0, 0.2);
  transition: transform 0.3s ease;
`;

/* 
   The individual buttons for each segment.
   We set them to flex so each one evenly uses 1/3 of the width in the wrapper
*/
const ButtonRow = styled.div`
  display: flex;
  width: 100%;
`;

/* 
   Each button is transparent so the indicator can show behind it 
*/
const ToggleButton = styled.button<{ active?: boolean }>`
  flex: 1;
  position: relative;
  z-index: 1; /* Above the indicator */
  background: transparent;
  border: none;
  color: ${(props) => (props.active ? "#007aff" : "#8e8e93")};
  font-size: 14px;
  padding: 8px 0;
  cursor: pointer;
  border-radius: 25px; /* for small rounding on the text area itself */

  &:hover {
    opacity: 0.8;
  }

  &:focus {
    outline: none;
  }
`;

function App() {
  const [viewMode, setViewMode] = useState<
    "sideBySide" | "frontFocus" | "chaseFocus"
  >("sideBySide");

  // For convenience, let's make an array of modes in the order we want them displayed
  const modes = ["sideBySide", "frontFocus", "chaseFocus"] as const;
  // We can map them to nice labels:
  const labels: Record<(typeof modes)[number], string> = {
    sideBySide: "Side By Side",
    frontFocus: "Front Focus",
    chaseFocus: "Chase Focus",
  };
  // Figure out which index we are on for the sliding indicator
  const currentIndex = modes.indexOf(viewMode);

  return (
    <Container className="App">
      <h1>My Simulation Viewer</h1>
      <PreviewContainer viewMode={viewMode}>
        {viewMode === "sideBySide" && (
          <>
            <MainImage
              viewMode={viewMode}
              src="http://localhost:8000/video_feed"
              alt="First Person"
            />
            <SecondaryImage
              viewMode={viewMode}
              src="http://localhost:8000/video_feed_chase"
              alt="Chase Camera"
            />
          </>
        )}
        {viewMode === "frontFocus" && (
          <>
            <MainImage
              viewMode={viewMode}
              src="http://localhost:8000/video_feed"
              alt="Front Camera Large"
            />
            <SecondaryImage
              viewMode={viewMode}
              src="http://localhost:8000/video_feed_chase"
              alt="Chase Camera Small"
            />
          </>
        )}
        {viewMode === "chaseFocus" && (
          <>
            <MainImage
              viewMode={viewMode}
              src="http://localhost:8000/video_feed_chase"
              alt="Chase Camera Large"
            />
            <SecondaryImage
              viewMode={viewMode}
              src="http://localhost:8000/video_feed"
              alt="Front Camera Small"
            />
          </>
        )}
      </PreviewContainer>

      {/* The "iOS-inspired" segmented control */}
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
    </Container>
  );
}

export default App;
