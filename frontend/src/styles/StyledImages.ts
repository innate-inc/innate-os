/**
 * Shared styled components for image display
 */

import styled from "styled-components";

export const HEIGHT_IMAGE_DISPLAY = 600;

/**
 * A fixed-size container for our "canvas" (1280×800).
 */
export const PreviewContainer = styled.div`
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
export const MainImage = styled.img<{ viewMode: string }>`
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
          left: calc((1280px - 800px) / 2);
          top: calc((${HEIGHT_IMAGE_DISPLAY}px - 600px) / 2);
          width: 800px;
          height: ${HEIGHT_IMAGE_DISPLAY}px;
        `;
      case "chaseFocus":
        return `
          /* chaseFocus => chase camera is large, 800×600 centered */
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
export const SecondaryImage = styled.img<{ viewMode: string }>`
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
          top: calc((${HEIGHT_IMAGE_DISPLAY}px - 600px) / 2 + 600px - 180px);
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
