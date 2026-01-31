import { useState } from "react";
import styled from "styled-components";
import "./App.css";
import { ImageDisplay } from "./components/ImageDisplay";
import { Chat } from "./components/Chat";

const Title = styled.h1`
  font-size: 22px;
  font-weight: ${({ theme }) => theme.fontWeights.bold};
  margin-bottom: 1.5rem;
`;

const Container = styled.div`
  height: calc(100vh - 2rem);
  display: flex;
  flex-direction: column;
  justify-content: center;
  overflow: hidden;
  text-align: center;
  padding: 1rem;
  background-color: ${({ theme }) => theme.colors.background};
  color: ${({ theme }) => theme.colors.foreground};
`;

const TopSection = styled.div`
  flex-shrink: 0;
  margin-top: 1rem;
`;

const ChatSection = styled.div`
  flex: 1;
  overflow: hidden;
  display: flex;
  flex-direction: column;
`;

const VersionBadge = styled.div`
  position: absolute;
  bottom: 0;
  right: 0;
  margin: 8px;
  font-size: 12px;
  opacity: 0.7;
  color: ${({ theme }) => theme.colors.muted};
`;

export default function App() {
  const [viewMode, setViewMode] = useState<
    "sideBySide" | "frontFocus" | "chaseFocus"
  >("sideBySide");

  async function handleResetRobot(memory_state?: string) {
    try {
      const baseUrl =
        import.meta.env.VITE_SIM_BASE_URL ?? "http://localhost:8000";

      const headers: Record<string, string> = {
        "Content-Type": "application/json",
      };

      // Check if memory_state is a string - if it's an event object or other non-string, don't use it
      const isValidMemoryState = typeof memory_state === "string";

      // Prepare the request body - always send a JSON object
      const body = isValidMemoryState
        ? JSON.stringify({ memory_state })
        : JSON.stringify({});

      const response = await fetch(`${baseUrl}/reset_robot`, {
        method: "POST",
        headers,
        body,
      });

      const data = await response.json();
      console.log("Reset response:", data);
      if (data.status === "reset_enqueued") {
        if (isValidMemoryState && memory_state) {
          alert(`Robot reset requested with memory state: ${memory_state}!`);
        } else {
          alert("Robot reset requested!");
        }
      }
    } catch (error) {
      console.error("Error resetting robot:", error);
    }
  }

  async function handleSetDirective(directive: string) {
    try {
      const baseUrl =
        import.meta.env.VITE_SIM_BASE_URL ?? "http://localhost:8000";

      const headers: Record<string, string> = {
        "Content-Type": "application/json",
      };

      const response = await fetch(`${baseUrl}/set_directive`, {
        method: "POST",
        headers,
        body: JSON.stringify({ text: directive }),
      });

      const data = await response.json();
      console.log("Directive response:", data);
    } catch (error) {
      console.error("Error setting directive:", error);
    }
  }

  return (
    <Container className="App">
      <TopSection>
        <Title>Innate Simulator</Title>
        <ImageDisplay
          viewMode={viewMode}
          setViewMode={setViewMode}
          onResetRobot={handleResetRobot}
          onSetDirective={handleSetDirective}
        />
      </TopSection>

      <ChatSection>
        <Chat onSetDirective={handleSetDirective} />
      </ChatSection>

      <VersionBadge>v0.1.0</VersionBadge>
    </Container>
  );
}
