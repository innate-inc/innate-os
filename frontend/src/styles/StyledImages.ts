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
          /* Fill half the container's width, then center vertically if desired. */
          left: 0;
          top: 0;
          width: 50%;
          height: 100%;
          object-fit: cover;
        `;
      case "frontFocus":
        return `
          /* Large front camera: 800×600 ratio roughly equals 4:3, but we can
             do a percentage-based approach to keep it flexible. For example,
             62.5% of container width is "800/1280=0.625". */
          left: 50%;
          top: 50%;
          transform: translate(-50%, -50%);
          width: 62.5%;
          height: auto; /* or 75% if you want 800/600 ratio, etc. */
          object-fit: cover;
        `;
      case "chaseFocus":
        return `
          /* Similarly, large chase camera. Same approach as frontFocus. */
          left: 50%;
          top: 50%;
          transform: translate(-50%, -50%);
          width: 62.5%;
          height: auto;
          object-fit: cover;
        `;
      default:
        return `
          /* fallback if needed */
          width: 100%;
          height: 100%;
          object-fit: cover;
        `;
    }
  }}

  @media (max-width: 768px) {
    /* On smaller screens, we might just go full width or do more
       "stacking" at this point. */
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
          /* Smaller chase camera pinned corner. We can pick a percentage
             of the container's width for consistency. Example: 20%. */
          width: 20%;
          height: auto;
          right: 0; /* or calculate a pinned offset */
          bottom: 0;
          object-fit: cover;
        `;
      case "chaseFocus":
        return `
          /* Smaller front camera pinned corner. */
          width: 20%;
          height: auto;
          right: 0;
          bottom: 0;
          object-fit: cover;
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
          /* Keep pinned corner effect, just scale the width if desired */
          width: 30vw;
          height: auto;
          right: 8px;
          bottom: 8px;
          left: auto;
          top: auto;
          position: absolute;
        `;
      } else {
        /* If sideBySide or default, do a stacked approach */
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
