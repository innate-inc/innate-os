import styled from "styled-components";

/**
 * Instead of a hard-coded 600, let's define an aspect ratio for 1280×600.
 */
export const PreviewContainer = styled.div`
  position: relative;
  /* Make it full-width but max at 1280px. */
  width: 60vw;
  max-width: 1280px;
  margin: 0 auto;
  /* Use the aspect-ratio property so that the height scales fluidly
     together with the width. */
  aspect-ratio: 1280 / 600;
  overflow: hidden;

  /* Or, if you need to support older browsers that don't have aspect-ratio,
     you could do something like:
     
     &::before {
       content: "";
       display: block;
       padding-bottom: calc(600 / 1280 * 100%);
     }
  */

  /* If you want to further handle especially tall/small screens, you could
     clamp the max-height or do a "vh-based" approach:
     max-height: 80vh;
     height: 80vh;
  */

  @media (max-width: 768px) {
    /* On small screens, we keep it flexible, or further reduce height if desired. */
    max-width: 100%;
    /* You might remove or alter aspect-ratio if smaller screens need a different approach. */
  }
`;

/**
 * For demonstration: The MainImage uses percentages so that it
 * scales automatically with the container's size.
 */
export const MainImage = styled.img<{ $viewMode: string }>`
  position: absolute;
  transition: all 0.8s ease-in-out;
  z-index: 1; /* behind secondary */

  ${({ $viewMode }) => {
    switch ($viewMode) {
      case "sideBySide":
        return `
          /* Fill half the container's width with a white divider on the right */
          left: 0;
          top: 0;
          width: 50%;
          height: 100%;
          object-fit: cover;
          border-right: 1px solid white;
          z-index: 100;
        `;
      case "frontFocus":
        return `
          /* Large front camera: 62.5% width centered */
          left: 50%;
          top: 50%;
          transform: translate(-50%, -50%);
          width: 62.5%;
          height: auto;
          object-fit: cover;
        `;
      case "chaseFocus":
        return `
          /* Similar approach as frontFocus */
          left: 50%;
          top: 50%;
          transform: translate(-50%, -50%);
          width: 62.5%;
          height: auto;
          object-fit: cover;
        `;
      default:
        return `
          /* Fallback */
          width: 100%;
          height: 100%;
          object-fit: cover;
        `;
    }
  }}

  @media (max-width: 768px) {
    /* On smaller screens, use a relative positioning */
    position: relative;
    left: 0;
    top: 0;
    transform: none;
    width: 100%;
    height: auto;
  }
`;

/**
 * Similarly, the SecondaryImage uses percentages and pinned corners if needed.
 */
export const SecondaryImage = styled.img<{ $viewMode: string }>`
  position: absolute;
  transition: all 0.8s ease-in-out;
  z-index: 2; /* above main */

  ${({ $viewMode }) => {
    switch ($viewMode) {
      case "sideBySide":
        return `
          /* Fill the right half of the container. */
          left: 50%;
          top: 0;
          width: 50%;
          height: 100%;
          object-fit: cover;
        `;
      case "frontFocus":
        return `
          /* Position the secondary image to align near the main image's right edge,
             add a slight offset, a border, and a subtle shadow. 
             Note:
             - The main image is centered with 62.5% width so its right edge is near 81.25%.
             - (100% - 62.5%) / 2 yields the natural gap (18.75%) on each side.
             - Adding 8px nudges its position inward. */
          width: 20%;
          height: auto;
          right: calc((100% - 62.5%) / 2 + 8px);
          bottom: 8px;
          object-fit: cover;
          border: 1px solid #ddd;
          box-shadow: 0 2px 4px rgba(0, 0, 0, 0.2);
        `;
      case "chaseFocus":
        return `
          /* Same as frontFocus – adjust the secondary image with a slight offset,
             border, and shadow. */
          width: 20%;
          height: auto;
          right: calc((100% - 62.5%) / 2 + 8px);
          bottom: 8px;
          object-fit: cover;
          border: 1px solid #ddd;
          box-shadow: 0 2px 4px rgba(0, 0, 0, 0.2);
        `;
      default:
        return `
          width: 100%;
          height: 100%;
          object-fit: cover;
        `;
    }
  }}

  @media (max-width: 768px) {
    ${({ $viewMode }) => {
      if ($viewMode === "frontFocus" || $viewMode === "chaseFocus") {
        return `
          /* On small screens, maintain a similar offset with slightly scaled adjustments */
          width: 30vw;
          height: auto;
          right: 8px;
          bottom: 8px;
          left: auto;
          top: auto;
          position: absolute;
          border: 1px solid #ddd;
          box-shadow: 0 2px 4px rgba(0, 0, 0, 0.2);
        `;
      } else {
        /* For sideBySide and default, use a stacked approach */
        return `
          position: relative;
          width: 100%;
          height: auto;
          margin: 0 auto;
        `;
      }
    }}
  }
`;
