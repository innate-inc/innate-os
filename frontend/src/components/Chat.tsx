/**
 * A simple placeholder Chat component
 */
import React from "react";
import styled from "styled-components";

const ChatContainer = styled.div`
  margin-top: 30px;
  width: 600px;
  margin-left: auto;
  margin-right: auto;
  text-align: left;
  border: 1px solid #ccc;
  padding: 10px;
  border-radius: 8px;
`;

export function Chat() {
  return (
    <ChatContainer>
      <h2>Chat</h2>
      <p>This is where your chat will go!</p>
    </ChatContainer>
  );
}
