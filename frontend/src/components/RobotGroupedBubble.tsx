import { useState, useRef, useEffect } from "react";
import styled, { keyframes, css } from "styled-components";

// New keyframes for animating the gradient background inside the text's pseudo-element.
const textWaveAnimation = keyframes`
  0% {
    background-position: 150% 0;
  }
  100% {
    background-position: -150% 0;
  }
`;

const RobotExtrasBubble = styled.div`
  font-style: italic;
  color: #888;
  font-size: 12px;
  padding: 0px 12px;
  align-self: flex-start;
  background: transparent;
  border-radius: 10px;

  @media (prefers-color-scheme: dark) {
    color: #bbb;
  }
`;

/* Simplified ToggleDiv without a background animation */
const ToggleDiv = styled.div`
  cursor: pointer;
  display: flex;
  flex-direction: row;
  justify-content: flex-start;
  align-items: flex-start;
  position: relative;
  overflow: hidden;
`;

const ArrowSpan = styled.span`
  margin-right: 4px;
`;

/*
  StatusSpan wraps the status text.
  When isLast is true, it adds a ::before pseudo-element that duplicates the text (via data-text)
  and uses background-clip: text to animate a white wave through the text.
*/
const StatusSpan = styled.span<{ isLast?: boolean }>`
  position: relative;
  display: inline-block;
  color: ${({ isLast }) => (isLast ? "#666" : "inherit")};
  padding-right: ${({ isLast }) => (isLast ? "20px" : "0")};

  ${({ isLast }) =>
    isLast &&
    css`
      &::before {
        content: attr(data-text);
        position: absolute;
        top: 0;
        left: 0;
        height: 100%;
        background: linear-gradient(
          90deg,
          transparent 0%,
          rgba(255, 255, 255, 0.3) 40%,
          transparent 80%
        );
        background-size: 200% auto;
        animation: ${textWaveAnimation} 3s linear infinite;
        -webkit-background-clip: text;
        background-clip: text;
        color: transparent;
        pointer-events: none;
        mix-blend-mode: screen;
      }
    `}
`;

/*
  Instead of conditionally rendering the content, we always render the container and use a 
  transition on its max-height (and margin-top) to animate open/close. We pass it the isOpen 
  state and the computed content height.
*/
const ContentDiv = styled.div<{
  isOpen: boolean;
  contentHeight: number;
}>`
  text-align: left;
  overflow: hidden;
  max-height: ${({ isOpen, contentHeight }) =>
    isOpen ? `${contentHeight}px` : "0px"};
  margin-top: ${({ isOpen }) => (isOpen ? "8px" : "0")};
  transition: max-height 0.3s ease, margin-top 0.3s ease;
`;

/*
  The inner container (InnerContent) holds the actual mapped content.
  We use a ref on this inner container to measure its scrollHeight.
*/
const InnerContent = styled.div``;

const ExtraItem = styled.div`
  margin-bottom: 4px;
  text-align: left;
`;

interface RobotGroupedBubbleProps {
  groupedExtras: string[];
  durationSeconds: number;
  isLast: boolean;
}

export const RobotGroupedBubble = ({
  groupedExtras,
  durationSeconds,
  isLast,
}: RobotGroupedBubbleProps) => {
  const [isOpen, setIsOpen] = useState(false);
  const [contentHeight, setContentHeight] = useState(0);
  const innerRef = useRef<HTMLDivElement>(null);

  // Update the content height on extras or open state changes.
  useEffect(() => {
    if (innerRef.current) {
      setContentHeight(innerRef.current.scrollHeight);
    }
  }, [groupedExtras, isOpen]);

  const workingOrWorked = !isLast ? "Thought" : "Thinking";
  const statusText =
    durationSeconds > 0
      ? `${workingOrWorked} for ${durationSeconds} seconds`
      : `${workingOrWorked}...`;

  return (
    <RobotExtrasBubble>
      <ToggleDiv onClick={() => setIsOpen((prev) => !prev)}>
        <ArrowSpan>{isOpen ? "▲" : "▼"}</ArrowSpan>
        <StatusSpan isLast={isLast} data-text={statusText}>
          {statusText}
        </StatusSpan>
      </ToggleDiv>
      <ContentDiv isOpen={isOpen} contentHeight={contentHeight}>
        <InnerContent ref={innerRef}>
          {groupedExtras.map((extra, index) => (
            <ExtraItem key={index}>{extra}</ExtraItem>
          ))}
        </InnerContent>
      </ContentDiv>
    </RobotExtrasBubble>
  );
};
