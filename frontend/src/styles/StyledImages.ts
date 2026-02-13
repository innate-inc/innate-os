import styled, { css } from "styled-components";

/**
 * Preview container maintains 640:480 (4:3) aspect ratio and fills available space
 */
export const PreviewContainer = styled.div`
  display: flex;
  flex-direction: column;
  position: relative;
  width: 100%;
  height: 100%;
  max-width: calc((100vh - 200px) * 4 / 3); /* Height-based max width for 4:3 */
  aspect-ratio: 640 / 480;
  overflow: hidden;
  background: #0a0a0a;
`;

// Define the shared styles as a function to avoid TypeScript errors
const getMainImageStyles = ($viewMode: string) => css`
  position: absolute;
  transition: all 0.8s ease-in-out;
  z-index: 10;

  ${() => {
    switch ($viewMode) {
      case "sideBySide":
        return css`
          left: 0;
          top: 0;
          width: 50%;
          height: 100%;
          object-fit: cover;
          border-right: 1px solid white;
        `;
      case "frontFocus":
      case "chaseFocus":
      default:
        return css`
          /* Fill the entire viewport without cropping */
          top: 0;
          left: 0;
          width: 100%;
          height: 100%;
          object-fit: contain;
        `;
    }
  }}

  @media (max-width: 768px) {
    position: relative;
    left: 0;
    top: 0;
    transform: none;
    width: 100%;
    height: auto;
  }
`;

const getSecondaryImageStyles = ($viewMode: string) => css`
  position: absolute;
  transition: all 0.8s ease-in-out;
  z-index: 11;
  object-fit: cover;

  ${() => {
    switch ($viewMode) {
      case "sideBySide":
        return css`
          left: 50%;
          top: 0;
          width: 50%;
          height: 100%;
          object-fit: cover;
        `;
      case "frontFocus":
      case "chaseFocus":
      default:
        return css`
          /* Picture-in-picture style in corner */
          width: 25%;
          height: auto;
          right: 16px;
          bottom: 16px;
          object-fit: cover;
          border: 1px solid rgba(255, 255, 255, 0.3);
          box-shadow: 4px 4px 0 rgba(255, 255, 255, 0.05);
        `;
    }
  }}

  @media (max-width: 768px) {
    ${() => {
      if ($viewMode === "frontFocus" || $viewMode === "chaseFocus") {
        return css`
          width: 30%;
          height: auto;
          right: 8px;
          bottom: 8px;
          left: auto;
          top: auto;
          position: absolute;
          border: 1px solid rgba(255, 255, 255, 0.3);
        `;
      } else {
        return css`
          position: relative;
          width: 100%;
          height: auto;
          margin: 0 auto;
        `;
      }
    }}
  }
`;

// Export the styled components with proper typing
export const MainImage = styled.img<{ $viewMode: string }>`
  ${({ $viewMode }) => getMainImageStyles($viewMode)}
`;

export const SecondaryImage = styled.img<{ $viewMode: string }>`
  ${({ $viewMode }) => getSecondaryImageStyles($viewMode)}
`;

// Export the styled video components
export const MainVideo = styled.video<{ $viewMode: string }>`
  ${({ $viewMode }) => getMainImageStyles($viewMode)}
`;

export const SecondaryVideo = styled.video<{ $viewMode: string }>`
  ${({ $viewMode }) => getSecondaryImageStyles($viewMode)}
`;
