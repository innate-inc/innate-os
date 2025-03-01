/**
 * A Chat component replicating the style you requested.
 */
import { useState, useEffect, useRef } from "react";
import styled from "styled-components";
// Example icon from react-icons (feel free to use your own icon or an SVG):
import { IoSend, IoPerson, IoHardwareChip } from "react-icons/io5";
import { RobotGroupedBubble } from "./RobotGroupedBubble";
import { groupMessages, Message, DisplayMessage } from "../utils/groupMessages";

const ChatContainer = styled.div`
  /* Default desktop width */
  width: 800px;
  height: 100%;
  display: flex;
  flex-direction: column;
  align-self: center;
  font-family: "Poppins", system-ui, -apple-system, BlinkMacSystemFont,
    sans-serif;

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
  gap: 12px;
`;

interface MessageBubbleProps {
  $isUser: boolean;
}

const MessageBubble = styled.div<MessageBubbleProps>`
  max-width: 80%;
  background: ${({ $isUser }) =>
    $isUser ? "linear-gradient(135deg, #2563eb 0%, #4f46e5 100%)" : "#ffffff"};
  color: ${({ $isUser }) => ($isUser ? "#ffffff" : "#333333")};
  border: ${({ $isUser }) => ($isUser ? "none" : "1px solid #e5e7eb")};
  border-radius: 18px;
  border-bottom-left-radius: ${({ $isUser }) => ($isUser ? "18px" : "0")};
  border-bottom-right-radius: ${({ $isUser }) => ($isUser ? "0" : "18px")};
  padding: 12px 16px;
  margin-bottom: 8px;
  align-self: ${({ $isUser }) => ($isUser ? "flex-end" : "flex-start")};
  text-align: ${({ $isUser }) => ($isUser ? "right" : "left")};
  font-size: 15px;
  line-height: 1.5;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.05);

  @media (prefers-color-scheme: dark) {
    background: ${({ $isUser }) =>
      $isUser
        ? "linear-gradient(135deg, #2563eb 0%, #4f46e5 100%)"
        : "#1e293b"};
    color: ${({ $isUser }) => ($isUser ? "#ffffff" : "#e5e7eb")};
    border: ${({ $isUser }) => ($isUser ? "none" : "1px solid #374151")};
  }
`;

const MessageSender = styled.div<{ $isUser: boolean }>`
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 4px;
  color: ${({ $isUser }) => ($isUser ? "#f0f4ff" : "#4b5563")};
  display: flex;
  align-items: center;
  gap: 6px;

  @media (prefers-color-scheme: dark) {
    color: ${({ $isUser }) => ($isUser ? "#f0f4ff" : "#9ca3af")};
  }
`;

const InputArea = styled.div`
  flex-shrink: 0;
  display: flex;
  align-items: center;
  padding: 12px 16px;
  background: #ffffff;
  border-radius: 20px;
  margin: 12px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.05);

  @media (prefers-color-scheme: dark) {
    background: #1e293b;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
  }
`;

const TextInput = styled.input`
  flex: 1;
  border: none;
  border-radius: 16px;
  padding: 12px;
  outline: none;
  background: transparent;
  font-size: 15px;
  ::placeholder {
    color: #94a3b8;
  }
  @media (prefers-color-scheme: dark) {
    color: #e5e7eb;
    ::placeholder {
      color: #64748b;
    }
  }
`;

const SendButton = styled.button`
  background: linear-gradient(135deg, #2563eb 0%, #4f46e5 100%);
  border: none;
  border-radius: 50%;
  width: 40px;
  height: 40px;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  margin-left: 8px;
  color: white;
  transition: transform 0.2s ease;

  &:hover {
    transform: scale(1.05);
  }

  /* Add styling for the SVG icon */
  svg {
    display: block;
    padding: 0;
    margin: 0;
  }
`;

export function Chat() {
  const [messages, setMessages] = useState<Message[]>([
    {
      sender: "robot",
      text: "Hello! I'm your robot assistant. How can I help you today?",
      timestamp: Date.now() - 5000,
    },
    {
      sender: "robot_thoughts",
      text: "Analyzing environment...",
      timestamp: Date.now() - 4000,
    },
    {
      sender: "robot_anticipation",
      text: "Ready to receive commands",
      timestamp: Date.now() - 3000,
    },
  ]);
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
                isLast={idx === groupedMessages.length - 1}
                groupedExtras={m.groupedExtras}
                durationSeconds={m.durationSeconds}
              />
            );
          }
          return (
            <MessageBubble key={idx} $isUser={m.sender === "user"}>
              <MessageSender $isUser={m.sender === "user"}>
                {m.sender === "user" ? (
                  <>
                    <span>You</span>
                    <IoPerson size={14} />
                  </>
                ) : (
                  <>
                    <IoHardwareChip size={14} />
                    <span>Robot</span>
                  </>
                )}
              </MessageSender>
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
          <IoSend size={20} style={{ display: "block", padding: 0 }} />
        </SendButton>
      </InputArea>
    </ChatContainer>
  );
}
