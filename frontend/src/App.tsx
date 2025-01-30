import { useState } from "react";
import styled from "styled-components";
import "./App.css";
import { ImageDisplay } from "./components/ImageDisplay";
import { ToggleViewMode } from "./components/ToggleViewMode";
import { Chat } from "./components/Chat";

const Title = styled.h1`
  font-size: 24px;
  font-weight: bold;
`;

const Container = styled.div`
  height: calc(100vh - 2rem);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  text-align: center;
  padding: 1rem;
`;

const TopSection = styled.div`
  flex-shrink: 0;
`;

const ChatSection = styled.div`
  flex: 1;
  overflow: hidden;
  display: flex;
  flex-direction: column;
`;

export default function App() {
  const [viewMode, setViewMode] = useState<
    "sideBySide" | "frontFocus" | "chaseFocus"
  >("sideBySide");

  return (
    <Container className="App">
      <TopSection>
        <Title>Innate Robot Operator</Title>
        <ImageDisplay viewMode={viewMode} />
        <ToggleViewMode viewMode={viewMode} setViewMode={setViewMode} />
      </TopSection>
      <ChatSection>
        <Chat />
      </ChatSection>
    </Container>
  );
}
