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
  position: relative;
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

const VersionBadge = styled.div`
  position: absolute;
  bottom: 0;
  right: 0;
  margin: 8px;
  font-size: 12px;
  opacity: 0.7;
  @media (prefers-color-scheme: dark) {
    color: #fff;
  }
`;

export default function App() {
  // A very simple password approach (do not use in production)
  const CORRECT_PASSWORD = "lol";
  const [enteredPassword, setEnteredPassword] = useState("");
  const [isAuthenticated, setIsAuthenticated] = useState(false);

  // For existing logic
  const [viewMode, setViewMode] = useState<
    "sideBySide" | "frontFocus" | "chaseFocus"
  >("sideBySide");

  function handlePasswordSubmit() {
    if (enteredPassword === CORRECT_PASSWORD) {
      setIsAuthenticated(true);
    } else {
      alert("Incorrect password. Please try again.");
      setEnteredPassword("");
    }
  }

  if (!isAuthenticated && import.meta.env.VITE_REQUIRE_AUTH === "true") {
    return (
      <Container>
        <TopSection>
          <Title>Please Enter Password</Title>
        </TopSection>
        <div style={{ margin: "auto" }}>
          <input
            type="password"
            placeholder="Password"
            value={enteredPassword}
            onChange={(e) => setEnteredPassword(e.target.value)}
          />
          <button onClick={handlePasswordSubmit}>Submit</button>
        </div>
      </Container>
    );
  }

  // If authenticated, show the simulator
  return (
    <Container className="App">
      <TopSection>
        <Title>Innate Simulator</Title>
        <ImageDisplay viewMode={viewMode} />
        <ToggleViewMode viewMode={viewMode} setViewMode={setViewMode} />
      </TopSection>
      <ChatSection>
        <Chat />
      </ChatSection>
      <VersionBadge>
        v{__APP_VERSION__} - c.{__COMMIT_HASH__}
      </VersionBadge>
    </Container>
  );
}
