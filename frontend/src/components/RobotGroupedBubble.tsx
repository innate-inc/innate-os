import { useState, useRef, useEffect } from "react";
import styled from "styled-components";

const RobotExtrasBubble = styled.div`
  font-style: italic;
  color: #888;
  font-size: 12px;
  padding: 8px 12px;
  margin-bottom: 10px;
  align-self: flex-start;
  background: transparent;
  border-radius: 10px;

  @media (prefers-color-scheme: dark) {
    color: #bbb;
  }
`;

const ToggleDiv = styled.div`
  cursor: pointer;
  display: flex;
  flex-direction: row;
  justify-content: flex-start;
  align-items: flex-start;
`;

const ArrowSpan = styled.span`
  margin-right: 4px;
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

  // Whenever the extras or open state change, update the content height value.
  // This allows the max-height transition to smoothly animate to the measured height.
  useEffect(() => {
    if (innerRef.current) {
      setContentHeight(innerRef.current.scrollHeight);
    }
  }, [groupedExtras, isOpen]);

  const workingOrWorked = !isLast ? "Thought" : "Thinking";

  return (
    <RobotExtrasBubble>
      <ToggleDiv onClick={() => setIsOpen((prev) => !prev)}>
        <ArrowSpan>{isOpen ? "▲" : "▼"}</ArrowSpan>
        <span>
          {durationSeconds > 0
            ? `${workingOrWorked} for ${durationSeconds} seconds`
            : `${workingOrWorked}...`}
        </span>
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
