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

  @media (max-width: 1024px) {
    max-width: 100%;
    aspect-ratio: auto;
    width: 100%;
  }
`;

// Define the shared styles as a function to avoid TypeScript errors
const getMainImageStyles = (_viewMode: string) => {
  void _viewMode;
  return css`
    position: absolute;
    transition: all 0.8s ease-in-out;
    z-index: 10;

    ${() => {
      return css`
        /* Fill the entire viewport without cropping */
        top: 0;
        left: 0;
        width: 100%;
        height: 100%;
        object-fit: contain;
      `;
    }}

    @media (max-width: 1024px) {
      position: relative;
      left: 0;
      top: 0;
      transform: none;
      width: 100%;
      height: auto;
    }
  `;
};

const getSecondaryImageStyles = (_viewMode: string) => {
  void _viewMode;
  return css`
    position: absolute;
    transition: all 0.8s ease-in-out;
    z-index: 11;
    object-fit: cover;

    ${() => {
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
    }}

    @media (max-width: 1024px) {
      ${() => {
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
      }}
    }
  `;
};

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
  display: block;
  background: #000;
  min-width: 1px;
  min-height: 1px;
`;

export const SecondaryVideo = styled.video<{ $viewMode: string }>`
  ${({ $viewMode }) => getSecondaryImageStyles($viewMode)}
  display: block;
  background: #000;
  min-width: 1px;
  min-height: 1px;
`;
