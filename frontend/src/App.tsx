/**
 * Our main App component. It uses ImageDisplay, ToggleViewMode, and a placeholder Chat component.
 */
import React, { useState } from "react";
import styled from "styled-components";
import "./App.css";
import { ImageDisplay } from "./components/ImageDisplay";
import { ToggleViewMode } from "./components/ToggleViewMode";
import { Chat } from "./components/Chat";

const Container = styled.div`
  text-align: center;
`;

export default function App() {
  const [viewMode, setViewMode] = useState<
    "sideBySide" | "frontFocus" | "chaseFocus"
  >("sideBySide");

  return (
    <Container className="App">
      <h1>Innate Sim</h1>

      {/* Display the main & secondary images */}
      <ImageDisplay viewMode={viewMode} />

      {/* Toggle to switch between modes */}
      <ToggleViewMode viewMode={viewMode} setViewMode={setViewMode} />

      {/* Future chat component */}
      <Chat />
    </Container>
  );
}
