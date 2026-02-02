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
  align-self: flex-start;
  position: relative;
`;

const ToggleDiv = styled.div`
  cursor: pointer;
  display: flex;
  flex-direction: column;
  position: relative;
  padding: 8px 12px;
  background: ${({ theme }) => theme.colors.secondary};
  border: 1px solid ${({ theme }) => theme.colors.foreground};
  border-bottom-left-radius: 0;
  border-bottom-right-radius: 4px;
  box-shadow: 4px 4px 0 rgba(255, 255, 255, 0.05);
  transition: background-color 0.2s ease;

  &:hover {
    background: rgba(255, 255, 255, 0.05);
  }
`;

const ArrowSpan = styled.span`
  margin-left: 6px;
  font-size: 10px;
  opacity: 0.5;
`;

const StatusLabel = styled.div`
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  margin-bottom: 4px;
  opacity: 0.5;
  display: flex;
  align-items: center;
  gap: 6px;
`;

const StatusSpan = styled.span<{ $isLast?: boolean }>`
  position: relative;
  display: inline-block;
  color: ${({ theme }) => theme.colors.foreground};
  font-size: 13px;
  line-height: 1.5;

  ${({ $isLast }) =>
    $isLast &&
    css`
      &::after {
        content: "";
        display: inline-block;
        width: 6px;
        height: 6px;
        margin-left: 6px;
        border-radius: 50%;
        background-color: ${({ theme }) => theme.colors.primary};
        animation: ${pulseAnimation} 1.5s infinite ease-in-out;
      }
    `}
`;

const ContentDiv = styled.div<{
  $isOpen: boolean;
  $contentHeight: number;
}>`
  text-align: left;
  overflow: hidden;
  max-height: ${({ $isOpen, $contentHeight }) =>
    $isOpen ? `${$contentHeight}px` : "0px"};
  margin-top: ${({ $isOpen }) => ($isOpen ? "8px" : "0")};
  transition:
    max-height 0.3s ease,
    margin-top 0.3s ease;
  cursor: pointer;
`;

const InnerContent = styled.div`
  background: ${({ theme }) => theme.colors.secondary};
  padding: 12px;
  border: 1px solid rgba(255, 255, 255, 0.1);
  color: ${({ theme }) => theme.colors.foreground};
  font-size: 13px;
  line-height: 1.5;
  cursor: pointer;
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
    opacity: 0.5;
  }

  &:last-child {
    margin-bottom: 0;
  }
`;

const ProcessTime = styled.div`
  font-size: 11px;
  text-align: right;
  opacity: 0.5;
  margin-top: 8px;
  font-weight: 500;
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

  const toggleOpen = () => setIsOpen((prev) => !prev);

  return (
    <RobotExtrasBubble>
      <ToggleDiv onClick={toggleOpen}>
        <StatusLabel>
          Thoughts
          <ArrowSpan>{isOpen ? "▲" : "▼"}</ArrowSpan>
        </StatusLabel>
        <StatusSpan $isLast={isLast}>{statusText}</StatusSpan>
      </ToggleDiv>
      <ContentDiv
        $isOpen={isOpen}
        $contentHeight={contentHeight}
        onClick={isOpen ? toggleOpen : undefined}
      >
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
