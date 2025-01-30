/**
 * Our main App component. It uses ImageDisplay, ToggleViewMode, and a placeholder Chat component.
 */
import { useState } from "react";
import styled from "styled-components";
import "./App.css";
import { ImageDisplay } from "./components/ImageDisplay";
import { ToggleViewMode } from "./components/ToggleViewMode";
import { Chat } from "./components/Chat";

const Container = styled.div`
  /* Make the main container take up the entire viewport height */
  height: 100vh;
  /* Column layout so top stays pinned and chat can fill remaining space */
  display: flex;
  flex-direction: column;
  /* Turn off window-level scrolling; we'll scroll only inside the chat area */
  overflow: hidden;

  /* Overflow hidden can optionally be moved to body/html, but this works. */
  text-align: center;
`;

const TopSection = styled.div`
  /* This is your pinned area for the heading, image display, and toggle */
  flex-shrink: 0; /* ensure it doesn't squish if the chat grows tall */
`;

const ChatSection = styled.div`
  /* Fill remaining vertical space, and scroll inside only this area */
  flex: 1;
  /* Prevent scrolling in the parent; the nested Chat component handles its own scroll */
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
        <h1>Innate Sim</h1>
        <ImageDisplay viewMode={viewMode} />
        <ToggleViewMode viewMode={viewMode} setViewMode={setViewMode} />
      </TopSection>
      <ChatSection>
        <Chat />
      </ChatSection>
    </Container>
  );
}
