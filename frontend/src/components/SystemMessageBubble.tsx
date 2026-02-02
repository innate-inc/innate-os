import styled from "styled-components";
import { IoCog, IoWarning } from "react-icons/io5";

const SystemMessageBubbleContainer = styled.div<{ $isError?: boolean }>`
  max-width: 90%;
  padding: 8px 12px;
  align-self: flex-start;
  text-align: left;
  font-size: 13px;
  line-height: 1.5;
  display: inline-block;
  background: ${({ theme, $isError }) =>
    $isError ? "rgba(239, 68, 68, 0.1)" : theme.colors.secondary};
  color: ${({ theme, $isError }) =>
    $isError ? "#ef4444" : theme.colors.foreground};
  border: 1px solid
    ${({ theme, $isError }) => ($isError ? "#ef4444" : theme.colors.foreground)};
  border-bottom-left-radius: 0;
  border-bottom-right-radius: 4px;
  box-shadow: 4px 4px 0 rgba(255, 255, 255, 0.05);
`;

const SystemMessageSender = styled.div<{ $isError?: boolean }>`
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  margin-bottom: 4px;
  opacity: 0.5;
  display: flex;
  align-items: center;
  gap: 6px;
`;

const SystemMessageContent = styled.div`
  font-size: 13px;
  line-height: 1.5;
`;

// For long system messages, add these components
const SystemToggleContainer = styled.div`
  max-width: 90%;
  align-self: flex-start;
`;

const SystemToggleDiv = styled.div<{ $isError?: boolean }>`
  cursor: pointer;
  display: flex;
  flex-direction: column;
  position: relative;
  padding: 8px 12px;
  background: ${({ theme, $isError }) =>
    $isError ? "rgba(239, 68, 68, 0.1)" : theme.colors.secondary};
  border: 1px solid
    ${({ theme, $isError }) => ($isError ? "#ef4444" : theme.colors.foreground)};
  border-bottom-left-radius: 0;
  border-bottom-right-radius: 4px;
  box-shadow: 4px 4px 0 rgba(255, 255, 255, 0.05);
  transition: background-color 0.2s ease;

  &:hover {
    background: ${({ $isError }) =>
      $isError ? "rgba(239, 68, 68, 0.15)" : "rgba(255, 255, 255, 0.05)"};
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
  transition:
    max-height 0.3s ease,
    margin-top 0.3s ease;
  cursor: pointer;
`;

const SystemInnerContent = styled.div<{ $isError?: boolean }>`
  background: ${({ theme, $isError }) =>
    $isError ? "rgba(239, 68, 68, 0.05)" : theme.colors.secondary};
  padding: 12px;
  border: 1px solid
    ${({ $isError }) =>
      $isError ? "rgba(239, 68, 68, 0.3)" : "rgba(255, 255, 255, 0.1)"};
  color: ${({ theme }) => theme.colors.foreground};
  font-size: 13px;
  line-height: 1.5;
  cursor: pointer;
`;

const ArrowSpan = styled.span<{ $isError?: boolean }>`
  margin-left: 6px;
  font-size: 10px;
  opacity: 0.5;
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
            <ArrowSpan $isError={isError}>{isExpanded ? "▲" : "▼"}</ArrowSpan>
          </SystemMessageSender>
          <SystemMessageContent>
            {text.substring(0, 60)}...
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
