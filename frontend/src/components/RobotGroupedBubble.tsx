import { useState } from "react";
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

const ContentDiv = styled.div`
  margin-top: 8px;
  text-align: left;
`;

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

  const workingOrWorked = isLast ? "Worked" : "Working";

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
      {isOpen && (
        <ContentDiv>
          {groupedExtras.map((extra, index) => (
            <ExtraItem key={index}>{extra}</ExtraItem>
          ))}
        </ContentDiv>
      )}
    </RobotExtrasBubble>
  );
};
