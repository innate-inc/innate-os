import styled from "styled-components";
import { IoCog, IoWarning } from "react-icons/io5";

const SystemMessageBubbleContainer = styled.div<{ $isError?: boolean }>`
  max-width: 75%;
  background: ${(props) =>
    props.$isError ? "rgba(239, 68, 68, 0.08)" : "rgba(79, 70, 229, 0.08)"};
  color: #334155;
  border: 1px solid
    ${(props) =>
      props.$isError ? "rgba(239, 68, 68, 0.15)" : "rgba(79, 70, 229, 0.15)"};
  border-radius: 12px;
  padding: 10px 14px;
  margin-bottom: 8px;
  align-self: flex-start;
  display: flex;
  align-items: center;
  font-size: 14px;
  line-height: 1.4;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.05);

  @media (prefers-color-scheme: dark) {
    background: ${(props) =>
      props.$isError ? "rgba(239, 68, 68, 0.12)" : "rgba(79, 70, 229, 0.12)"};
    color: #94a3b8;
    border: 1px solid
      ${(props) =>
        props.$isError ? "rgba(239, 68, 68, 0.2)" : "rgba(79, 70, 229, 0.2)"};
  }
`;

const SystemMessageSender = styled.div<{ $isError?: boolean }>`
  font-size: 12px;
  font-weight: 600;
  margin-right: 12px;
  padding-right: 12px;
  border-right: 1px solid
    ${(props) =>
      props.$isError ? "rgba(239, 68, 68, 0.2)" : "rgba(79, 70, 229, 0.2)"};
  color: ${(props) => (props.$isError ? "#ef4444" : "#4f46e5")};
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 80px;

  @media (prefers-color-scheme: dark) {
    color: ${(props) => (props.$isError ? "#f87171" : "#818cf8")};
    border-right: 1px solid
      ${(props) =>
        props.$isError ? "rgba(239, 68, 68, 0.3)" : "rgba(79, 70, 229, 0.3)"};
  }
`;

const SystemMessageContent = styled.div`
  flex: 1;
  font-size: 14px;
  line-height: 1.4;
  text-align: center;
`;

// For long system messages, add these components
const SystemToggleContainer = styled.div`
  max-width: 75%;
  align-self: flex-start;
  margin-bottom: 8px;
`;

const SystemToggleDiv = styled.div<{ $isError?: boolean }>`
  cursor: pointer;
  display: flex;
  flex-direction: row;
  justify-content: flex-start;
  align-items: center;
  position: relative;
  padding: 10px 14px;
  background-color: ${(props) =>
    props.$isError ? "rgba(239, 68, 68, 0.08)" : "rgba(79, 70, 229, 0.08)"};
  border-radius: 12px;
  border: 1px solid
    ${(props) =>
      props.$isError ? "rgba(239, 68, 68, 0.15)" : "rgba(79, 70, 229, 0.15)"};
  transition: background-color 0.2s ease;

  &:hover {
    background-color: ${(props) =>
      props.$isError ? "rgba(239, 68, 68, 0.12)" : "rgba(79, 70, 229, 0.12)"};
  }

  @media (prefers-color-scheme: dark) {
    background-color: ${(props) =>
      props.$isError ? "rgba(239, 68, 68, 0.12)" : "rgba(79, 70, 229, 0.12)"};
    border: 1px solid
      ${(props) =>
        props.$isError ? "rgba(239, 68, 68, 0.2)" : "rgba(79, 70, 229, 0.2)"};

    &:hover {
      background-color: ${(props) =>
        props.$isError ? "rgba(239, 68, 68, 0.18)" : "rgba(79, 70, 229, 0.18)"};
    }
  }
`;

const SystemContentDiv = styled.div<{
  isOpen: boolean;
  contentHeight: number;
}>`
  overflow: hidden;
  max-height: ${({ isOpen, contentHeight }) =>
    isOpen ? `${contentHeight}px` : "0px"};
  margin-top: ${({ isOpen }) => (isOpen ? "8px" : "0")};
  transition: max-height 0.3s ease, margin-top 0.3s ease;
  cursor: pointer;
`;

const SystemInnerContent = styled.div<{ $isError?: boolean }>`
  background-color: ${(props) =>
    props.$isError ? "rgba(239, 68, 68, 0.05)" : "rgba(79, 70, 229, 0.05)"};
  border-radius: 12px;
  padding: 12px;
  border: 1px solid
    ${(props) =>
      props.$isError ? "rgba(239, 68, 68, 0.1)" : "rgba(79, 70, 229, 0.1)"};
  color: #4b5563;
  font-size: 14px;
  line-height: 1.4;
  cursor: pointer;

  @media (prefers-color-scheme: dark) {
    background-color: ${(props) =>
      props.$isError ? "rgba(239, 68, 68, 0.1)" : "rgba(79, 70, 229, 0.1)"};
    border: 1px solid
      ${(props) =>
        props.$isError ? "rgba(239, 68, 68, 0.2)" : "rgba(79, 70, 229, 0.2)"};
    color: #94a3b8;
  }
`;

const ArrowSpan = styled.span<{ $isError?: boolean }>`
  margin-left: 6px;
  font-size: 10px;
  color: ${(props) => (props.$isError ? "#ef4444" : "#4f46e5")};

  @media (prefers-color-scheme: dark) {
    color: ${(props) => (props.$isError ? "#f87171" : "#818cf8")};
  }
`;

interface SystemMessageBubbleProps {
  messageId: number;
  text: string;
  isExpanded: boolean;
  onToggleExpand: (messageId: number) => void;
  contentRef: (el: HTMLDivElement | null) => void;
  isError?: boolean;
}

export const SystemMessageBubble = ({
  messageId,
  text,
  isExpanded,
  onToggleExpand,
  contentRef,
  isError = false,
}: SystemMessageBubbleProps) => {
  const isLongMessage = text.length > 60;

  if (isLongMessage) {
    return (
      <SystemToggleContainer>
        <SystemToggleDiv
          $isError={isError}
          onClick={() => onToggleExpand(messageId)}
        >
          <SystemMessageSender $isError={isError}>
            {isError ? <IoWarning size={14} /> : <IoCog size={14} />}
            <span>System</span>
          </SystemMessageSender>
          <SystemMessageContent>
            {text.substring(0, 60)}...
            <ArrowSpan $isError={isError}>{isExpanded ? "▲" : "▼"}</ArrowSpan>
          </SystemMessageContent>
        </SystemToggleDiv>
        <SystemContentDiv
          isOpen={isExpanded}
          contentHeight={
            document.getElementById(`system-content-${messageId}`)
              ?.scrollHeight || 0
          }
        >
          <SystemInnerContent
            $isError={isError}
            id={`system-content-${messageId}`}
            ref={contentRef}
            onClick={() => onToggleExpand(messageId)}
          >
            {text}
          </SystemInnerContent>
        </SystemContentDiv>
      </SystemToggleContainer>
    );
  } else {
    return (
      <SystemMessageBubbleContainer $isError={isError}>
        <SystemMessageSender $isError={isError}>
          {isError ? <IoWarning size={14} /> : <IoCog size={14} />}
          <span>System</span>
        </SystemMessageSender>
        <SystemMessageContent>{text}</SystemMessageContent>
      </SystemMessageBubbleContainer>
    );
  }
};
