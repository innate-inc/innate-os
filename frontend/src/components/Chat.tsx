/**
 * A Chat component replicating the style you requested.
 */
import React, { useState } from "react";
import styled from "styled-components";
// Example icon from react-icons (feel free to use your own icon or an SVG):
import { IoSend } from "react-icons/io5";

const ChatContainer = styled.div`
  width: 600px;
  margin: 30px auto;
  border: none;
  border-radius: 0;
  background-color: transparent;
  display: flex;
  flex-direction: column;
  min-height: 500px;
`;

const MessagesWrapper = styled.div`
  flex: 1;
  padding: 16px;
  overflow-y: auto;
`;

interface MessageBubbleProps {
  isUser: boolean;
}

const MessageBubble = styled.div<MessageBubbleProps>`
  background: ${({ isUser }) => (isUser ? "#efefef" : "#ffffff")};
  border: none;
  border-radius: ${({ isUser }) => (isUser ? "20px" : "8px")};
  padding: 10px;
  margin-bottom: 10px;
  max-width: 70%;
  align-self: ${({ isUser }) => (isUser ? "flex-end" : "flex-start")};
  text-align: ${({ isUser }) => (isUser ? "right" : "left")};
`;

const InputArea = styled.div`
  display: flex;
  align-items: center;
  border-top: none;
  padding: 8px;
  background: #eee;
  border-radius: 16px;
`;

const TextInput = styled.input`
  flex: 1;
  border: none;
  border-radius: 16px;
  padding: 8px;
  outline: none;
  background: transparent;

  ::placeholder {
    color: #666; /* Slightly darker placeholder text */
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
