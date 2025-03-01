import { useState, useRef, useEffect } from "react";
import styled, { keyframes, css } from "styled-components";

// Animation for thinking indicator
const pulseAnimation = keyframes`
  0% { opacity: 0.5; }
  50% { opacity: 1; }
  100% { opacity: 0.5; }
`;

const RobotExtrasBubble = styled.div`
  max-width: 90%;
  font-size: 13px;
  padding: 1px;
  align-self: flex-start;
  background: transparent;
  border-radius: 10px;
  position: relative;
  margin-bottom: 4px;

  @media (prefers-color-scheme: dark) {
    color: #94a3b8;
  }
`;

const ToggleDiv = styled.div`
  cursor: pointer;
  display: flex;
  flex-direction: row;
  justify-content: flex-start;
  align-items: center;
  position: relative;
  padding: 8px 12px;
  background-color: rgba(79, 70, 229, 0.1);
  border-radius: 12px;
  border: 1px solid rgba(79, 70, 229, 0.2);
  transition: background-color 0.2s ease;

  &:hover {
    background-color: rgba(79, 70, 229, 0.15);
  }

  @media (prefers-color-scheme: dark) {
    background-color: rgba(79, 70, 229, 0.2);
    border: 1px solid rgba(79, 70, 229, 0.3);

    &:hover {
      background-color: rgba(79, 70, 229, 0.25);
    }
  }
`;

const ArrowSpan = styled.span`
  margin-right: 6px;
  font-size: 10px;
  color: #4f46e5;

  @media (prefers-color-scheme: dark) {
    color: #818cf8;
  }
`;

const StatusSpan = styled.span<{ isLast?: boolean }>`
  position: relative;
  display: inline-block;
  color: ${({ isLast }) => (isLast ? "#4f46e5" : "#6366f1")};
  font-weight: 500;

  ${({ isLast }) =>
    isLast &&
    css`
      &::after {
        content: "";
        display: inline-block;
        width: 6px;
        height: 6px;
        margin-left: 6px;
        border-radius: 50%;
        background-color: #4f46e5;
        animation: ${pulseAnimation} 1.5s infinite ease-in-out;
      }
    `}

  @media (prefers-color-scheme: dark) {
    color: ${({ isLast }) => (isLast ? "#818cf8" : "#6366f1")};

    ${({ isLast }) =>
      isLast &&
      css`
        &::after {
          background-color: #818cf8;
        }
      `}
  }
`;

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

const InnerContent = styled.div`
  background-color: rgba(79, 70, 229, 0.05);
  border-radius: 12px;
  padding: 12px;
  border: 1px solid rgba(79, 70, 229, 0.1);
  color: #4b5563;

  @media (prefers-color-scheme: dark) {
    background-color: rgba(79, 70, 229, 0.1);
    border: 1px solid rgba(79, 70, 229, 0.2);
    color: #94a3b8;
  }
`;

const ExtraItem = styled.div`
  margin-bottom: 8px;
  text-align: left;
  position: relative;
  padding-left: 12px;

  &:before {
    content: "•";
    position: absolute;
    left: 0;
    color: #6366f1;
  }

  &:last-child {
    margin-bottom: 0;
  }
`;

const ProcessTime = styled.div`
  font-size: 11px;
  text-align: right;
  color: #6366f1;
  margin-top: 8px;
  font-weight: 500;

  @media (prefers-color-scheme: dark) {
    color: #818cf8;
  }
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

  const workingOrWorked = !isLast ? "Processed" : "Processing";
  const statusText =
    durationSeconds > 0
      ? `${workingOrWorked} for ${durationSeconds} seconds`
      : `${workingOrWorked}...`;

  return (
    <RobotExtrasBubble>
      <ToggleDiv onClick={() => setIsOpen((prev) => !prev)}>
        <ArrowSpan>{isOpen ? "▲" : "▼"}</ArrowSpan>
        <StatusSpan isLast={isLast}>{statusText}</StatusSpan>
      </ToggleDiv>
      <ContentDiv isOpen={isOpen} contentHeight={contentHeight}>
        <InnerContent ref={innerRef}>
          {groupedExtras.map((extra, index) => (
            <ExtraItem key={index}>{extra}</ExtraItem>
          ))}
          <ProcessTime>Process time: {durationSeconds}s</ProcessTime>
        </InnerContent>
      </ContentDiv>
    </RobotExtrasBubble>
  );
};
