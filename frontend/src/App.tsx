import React, { useState } from "react";
import styled from "styled-components";
import "./App.css";

const HEIGHT_IMAGE_DISPLAY = 600;

const Container = styled.div`
  text-align: center;
`;

/**
 * A fixed-size container for our "canvas" (1280×800).
 */
const PreviewContainer = styled.div`
  position: relative;
  width: 1280px;
  height: ${HEIGHT_IMAGE_DISPLAY}px;
  margin: 0 auto;
  overflow: hidden;
`;

/**
 * One "Main" <img>, kept in the DOM at all times.
 * We transition all numeric properties for smooth moves.
 */
const MainImage = styled.img<{ viewMode: string }>`
  position: absolute;
  transition: all 0.8s ease-in-out;
  z-index: 1; /* behind secondary */

  ${({ viewMode }) => {
    switch (viewMode) {
      case "sideBySide":
        return `
          /* half the container (640×480), centered vertically */
          left: 0;
          top: calc((${HEIGHT_IMAGE_DISPLAY}px - 480px) / 2);
          width: 640px;
          height: ${(HEIGHT_IMAGE_DISPLAY * 480) / 640}px;
        `;
      case "frontFocus":
        return `
          /* frontFocus => front camera is large, 800×600, centered */
          left: calc((1280px - 800px) / 2); /* 240px */
          top: calc((${HEIGHT_IMAGE_DISPLAY}px - 600px) / 2);   /* 100px */
          width: 800px;
          height: ${HEIGHT_IMAGE_DISPLAY}px;
        `;
      case "chaseFocus":
        return `
          /* chaseFocus => chase camera is large (so main=chase), 800×600 centered */
          left: calc((1280px - 800px) / 2);
          top: calc((${HEIGHT_IMAGE_DISPLAY}px - 600px) / 2);
          width: 800px;
          height: ${HEIGHT_IMAGE_DISPLAY}px;
        `;
      default:
        return "";
    }
  }}
`;

/**
 * One "Secondary" <img>, also kept always in the DOM.
 */
const SecondaryImage = styled.img<{ viewMode: string }>`
  position: absolute;
  transition: all 0.8s ease-in-out;
  z-index: 2; /* above main */

  ${({ viewMode }) => {
    switch (viewMode) {
      case "sideBySide":
        return `
          left: 640px;
          top: calc((${HEIGHT_IMAGE_DISPLAY}px - 480px) / 2);
          width: 640px;
          height: ${(HEIGHT_IMAGE_DISPLAY * 480) / 640}px;
        `;
      case "frontFocus":
        return `
          /* small chase camera pinned bottom-right of the large front camera */
          width: 240px;
          height: 180px;
          left: calc((1280px - 800px) / 2 + 800px - 240px); 
               /* 240 + 800 - 240 = 800 */
          top: calc((${HEIGHT_IMAGE_DISPLAY}px - 600px) / 2 + 600px - 180px);
               /* 100 + 600 - 180 = 520 */
        `;
      case "chaseFocus":
        return `
          /* small front camera pinned bottom-right of the large chase camera */
          width: 240px;
          height: 180px;
          left: calc((1280px - 800px) / 2 + 800px - 240px);
          top: calc((${HEIGHT_IMAGE_DISPLAY}px - 600px) / 2 + 600px - 180px);
        `;
      default:
        return "";
    }
  }}
`;

/* The segmented toggle style bits */
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

/**
 * Our main App. We always render exactly one MainImage and one SecondaryImage.
 * We *swap* the src depending on "sideBySide / frontFocus / chaseFocus"
 * so the correct feed is considered "main".
 */
function App() {
  const [viewMode, setViewMode] = useState<
    "sideBySide" | "frontFocus" | "chaseFocus"
  >("sideBySide");

  // For convenience, array of modes in the order we want them displayed
  const modes = ["sideBySide", "frontFocus", "chaseFocus"] as const;
  const labels: Record<(typeof modes)[number], string> = {
    sideBySide: "Side By Side",
    frontFocus: "Front Focus",
    chaseFocus: "Chase Focus",
  };
  const currentIndex = modes.indexOf(viewMode);

  // We decide which feed is main vs secondary based on the mode:
  let mainSrc = "http://localhost:8000/video_feed";
  let subSrc = "http://localhost:8000/video_feed_chase";

  if (viewMode === "chaseFocus") {
    // chaseFocus => The chase camera becomes main feed
    mainSrc = "http://localhost:8000/video_feed_chase";
    subSrc = "http://localhost:8000/video_feed";
  } else if (viewMode === "frontFocus") {
    // frontFocus => The front camera is main, chase camera is sub
    mainSrc = "http://localhost:8000/video_feed";
    subSrc = "http://localhost:8000/video_feed_chase";
  }
  // sideBySide => The front camera is main, chase is sub

  return (
    <Container className="App">
      <h1>My Simulation Viewer</h1>

      <PreviewContainer>
        <MainImage viewMode={viewMode} src={mainSrc} alt="Main Camera" />
        <SecondaryImage viewMode={viewMode} src={subSrc} alt="Sub Camera" />
      </PreviewContainer>

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
