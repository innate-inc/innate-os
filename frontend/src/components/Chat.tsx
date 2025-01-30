/**
 * A Chat component replicating the style you requested.
 */
import { useState, useEffect, useRef } from "react";
import styled from "styled-components";
// Example icon from react-icons (feel free to use your own icon or an SVG):
import { IoSend } from "react-icons/io5";

const ChatContainer = styled.div`
  /* Fill all available space from the parent ChatSection */
  width: 800px;
  height: 100%;
  display: flex;
  flex-direction: column;
  align-self: center;
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
  isUser: boolean;
}

const MessageBubble = styled.div<MessageBubbleProps>`
  background: ${({ isUser }) => (isUser ? "#efefef" : "transparent")};
  border: none;
  border-radius: 20px;
  padding: 10px 15px;
  margin-bottom: 10px;
  align-self: ${({ isUser }) => (isUser ? "flex-end" : "flex-start")};
  text-align: ${({ isUser }) => (isUser ? "right" : "left")};

  @media (prefers-color-scheme: dark) {
    /* Dark-mode version of the bubbles */
    background: ${({ isUser }) => (isUser ? "#444" : "transparent")};
    color: #fff;
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

interface Message {
  text: string;
  sender: "user" | "robot";
}

export function Chat() {
  // Sample messages; you'll determine how to handle them in a real scenario.
  const [messages, setMessages] = useState<Message[]>([
    { text: "Hello", sender: "robot" },
  ]);
  const [draft, setDraft] = useState("");
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // Automatically scroll to the bottom when messages change
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const handleSend = () => {
    const cleanDraft = draft.trim();
    if (cleanDraft) {
      // For user messages:
      setMessages((prev) => [...prev, { text: cleanDraft, sender: "user" }]);
      setDraft("");

      // Optionally, simulate a robot response:
      // setTimeout(() => {
      //   setMessages((prev) => [...prev, { text: "Hello from the robot!", sender: "robot" }]);
      // }, 1000);
    }
  };

  return (
    <ChatContainer>
      <MessagesWrapper>
        {messages.map((m, idx) => (
          <MessageBubble key={idx} isUser={m.sender === "user"}>
            {m.text}
          </MessageBubble>
        ))}
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
            // Send on Enter
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
