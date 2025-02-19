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

interface RobotGroupedBubbleProps {
  groupedExtras: string[];
  durationSeconds: number;
}

export function RobotGroupedBubble({
  groupedExtras,
  durationSeconds,
}: RobotGroupedBubbleProps) {
  const [isOpen, setIsOpen] = useState(false);

  return (
    <RobotExtrasBubble>
      <div
        style={{ cursor: "pointer", display: "flex", alignItems: "center" }}
        onClick={() => setIsOpen((prev) => !prev)}
      >
        <span>
          {durationSeconds > 0
            ? `Thinking for ${durationSeconds} seconds`
            : "Thinking..."}
        </span>
        <span style={{ marginLeft: "auto" }}>{isOpen ? "▲" : "▼"}</span>
      </div>
      {isOpen && (
        <div style={{ marginTop: "8px" }}>
          {groupedExtras.map((extra, index) => (
            <div key={index} style={{ marginBottom: "4px" }}>
              {extra}
            </div>
          ))}
        </div>
      )}
    </RobotExtrasBubble>
  );
}
