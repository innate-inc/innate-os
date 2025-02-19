/**
 * A Chat component replicating the style you requested.
 */
import { useState, useEffect, useRef } from "react";
import styled from "styled-components";
// Example icon from react-icons (feel free to use your own icon or an SVG):
import { IoSend } from "react-icons/io5";
import { RobotGroupedBubble } from "./RobotGroupedBubble";
import { groupMessages, Message, DisplayMessage } from "../utils/groupMessages";

const ChatContainer = styled.div`
  /* Default desktop width */
  width: 800px;
  height: 100%;
  display: flex;
  flex-direction: column;
  align-self: center;

  /* On screens below 768px, take the full width */
  @media (max-width: 768px) {
    width: 100%;
  }
`;

const MessagesWrapper = styled.div`
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
`;

interface MessageBubbleProps {
  $isUser: boolean;
}

const MessageBubble = styled.div<MessageBubbleProps>`
  background: ${({ $isUser }) => ($isUser ? "#efefef" : "transparent")};
  border: none;
  border-radius: 20px;
  padding: 10px 15px;
  margin-bottom: 10px;
  align-self: ${({ $isUser }) => ($isUser ? "flex-end" : "flex-start")};
  text-align: ${({ $isUser }) => ($isUser ? "right" : "left")};
  font-size: 14px;
  line-height: 22px;

  @media (prefers-color-scheme: dark) {
    /* Dark-mode version of the bubbles */
    background: ${({ $isUser }) => ($isUser ? "#444" : "transparent")};
    color: #fff;
  }
`;

// This styled component is now used as the outer container for the grouped dropdown.
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

const InputArea = styled.div`
  /* Pinned at the bottom */
  flex-shrink: 0;
  display: flex;
  align-items: center;
  padding: 8px;
  background: #eee;
  border-radius: 16px;

  @media (prefers-color-scheme: dark) {
    background: #2a2a2a;
  }
`;

const TextInput = styled.input`
  flex: 1;
  border: none;
  border-radius: 16px;
  padding: 8px;
  outline: none;
  background: transparent;
  ::placeholder {
    color: #666;
  }
  @media (prefers-color-scheme: dark) {
    color: #fff;
    ::placeholder {
      color: #aaa;
    }
  }
`;

const SendButton = styled.button`
  background: none;
  border: none;
  cursor: pointer;
  margin-left: 8px;
  &:hover {
    opacity: 0.8;
  }
`;

export function Chat() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [isScrolledToBottom, setIsScrolledToBottom] = useState(true);
  const wsRef = useRef<WebSocket | null>(null);

  const handleScroll = () => {
    if (containerRef.current) {
      const { scrollTop, scrollHeight, clientHeight } = containerRef.current;
      setIsScrolledToBottom(scrollHeight - scrollTop - clientHeight < 10);
    }
  };

  useEffect(() => {
    // If there's a socket and it's not fully closed, skip making a new one.
    if (
      wsRef.current &&
      wsRef.current.readyState !== WebSocket.CLOSED &&
      wsRef.current.readyState !== WebSocket.CLOSING
    ) {
      return;
    }

    const wsUrl = `${import.meta.env.VITE_WS_BASE_URL}/ws/chat`;
    const socket = new WebSocket(wsUrl);
    wsRef.current = socket;

    socket.onopen = () => {
      console.log("Connected to chat websocket");
    };

    socket.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.sender && data.text) {
          setMessages((prev) => {
            // Check if an identical message already exists
            const duplicateExists = prev.some(
              (m) =>
                m.sender === data.sender &&
                m.text === data.text &&
                m.timestamp === data.timestamp
            );
            if (duplicateExists) {
              return prev;
            }
            // Add the new message and sort by timestamp in ascending order
            const newMessages = [
              ...prev,
              {
                sender: data.sender,
                text: data.text,
                timestamp: data.timestamp,
              },
            ];
            newMessages.sort((a, b) => a.timestamp - b.timestamp);
            return newMessages;
          });
        }
      } catch (err) {
        console.error("Invalid message received:", event.data);
      }
    };

    socket.onclose = () => {
      console.log("Chat websocket closed");
    };

    socket.onerror = (error) => {
      console.error("WebSocket error:", error);
    };

    return () => {
      socket.close();
    };
  }, []);

  useEffect(() => {
    if (isScrolledToBottom) {
      messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages, isScrolledToBottom]);

  const handleSend = () => {
    const cleanDraft = draft.trim();
    if (!cleanDraft || !wsRef.current) return;

    // Send the draft message to the server via WebSocket
    console.log("Sending message:", cleanDraft);
    wsRef.current.send(cleanDraft);

    // Clear the input
    setDraft("");
  };

  // Use the grouping utility to prepare messages for display.
  const groupedMessages: DisplayMessage[] = groupMessages(messages);

  return (
    <ChatContainer>
      <MessagesWrapper ref={containerRef} onScroll={handleScroll}>
        {groupedMessages.map((m, idx) => {
          if (m.sender === "robot_grouped") {
            return (
              <RobotGroupedBubble
                key={idx}
                groupedExtras={m.groupedExtras}
                durationSeconds={m.durationSeconds}
              />
            );
          }
          return (
            <MessageBubble key={idx} $isUser={m.sender === "user"}>
              {m.text}
            </MessageBubble>
          );
        })}
        {/* Marker element for auto-scroll */}
        <div ref={messagesEndRef} />
      </MessagesWrapper>
      <InputArea>
        <TextInput
          type="text"
          placeholder="Type your message..."
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              handleSend();
            }
          }}
        />
        <SendButton onClick={handleSend}>
          <IoSend size={24} />
        </SendButton>
      </InputArea>
    </ChatContainer>
  );
}
