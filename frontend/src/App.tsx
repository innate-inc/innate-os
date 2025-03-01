import { useState } from "react";
import styled from "styled-components";
import "./App.css";
import { ImageDisplay } from "./components/ImageDisplay";
import { ToggleViewMode } from "./components/ToggleViewMode";
import { Chat } from "./components/Chat";
import {
  MdRefresh,
  MdAutoMode,
  MdMood,
  MdTour,
  MdSecurity,
  MdHealthAndSafety,
} from "react-icons/md";

const Title = styled.h1`
  font-size: 24px;
  font-weight: bold;
  margin-bottom: 2rem;
`;

const Container = styled.div`
  height: calc(100vh - 2rem);
  display: flex;
  flex-direction: column;
  justify-content: center;
  overflow: hidden;
  text-align: center;
  padding: 1rem;
  position: relative;
`;

const TopSection = styled.div`
  flex-shrink: 0;
  margin-top: 2rem;
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

const ResetButton = styled.button`
  position: absolute;
  top: 10px;
  left: 10px;
  background-color: #007bff;
  border: none;
  padding: 8px 12px;
  color: white;
  border-radius: 4px;
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  cursor: pointer;
  transition: background 0.2s ease;

  &:hover {
    background-color: #0056b3;
  }
`;

const AuthContainer = styled.div`
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 1rem;
  margin-top: 2rem;
`;

const StyledPasswordInput = styled.input`
  width: 250px;
  padding: 10px;
  border: 2px solid #007bff;
  border-radius: 6px;
  font-size: 16px;
  outline: none;
  transition: border-color 0.3s ease;

  &:focus {
    border-color: #0056b3;
  }
`;

const StyledSubmitButton = styled.button`
  width: 150px;
  padding: 10px;
  background-color: #007bff;
  border: none;
  border-radius: 6px;
  color: #fff;
  font-size: 16px;
  cursor: pointer;
  transition: background-color 0.2s ease;

  &:hover {
    background-color: #0056b3;
  }
`;

const DirectiveButtonsContainer = styled.div`
  position: fixed;
  bottom: 80px;
  right: 20px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  z-index: 10;
`;

const DirectiveButton = styled.button`
  background: linear-gradient(135deg, #2563eb 0%, #4f46e5 100%);
  border: none;
  padding: 10px;
  color: white;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  cursor: pointer;
  transition: transform 0.2s ease, box-shadow 0.2s ease;
  box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
  width: 40px;
  height: 40px;

  &:hover {
    transform: translateY(-2px);
    box-shadow: 0 4px 8px rgba(0, 0, 0, 0.15);
  }

  @media (prefers-color-scheme: dark) {
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.3);

    &:hover {
      box-shadow: 0 4px 8px rgba(0, 0, 0, 0.4);
    }
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

  // Add these state variables inside the App component
  const [directiveText, setDirectiveText] = useState("");

  function handlePasswordSubmit() {
    if (enteredPassword === CORRECT_PASSWORD) {
      setIsAuthenticated(true);
    } else {
      alert("Incorrect password. Please try again.");
      setEnteredPassword("");
    }
  }

  async function handleResetRobot() {
    try {
      const baseUrl =
        import.meta.env.VITE_SIM_BASE_URL ?? "http://localhost:8000";

      const response = await fetch(`${baseUrl}/reset_robot`, {
        method: "POST",
      });
      const data = await response.json();
      console.log("Reset response:", data);
      if (data.status === "reset_enqueued") {
        alert("Robot reset requested!");
      }
    } catch (error) {
      console.error("Error resetting robot:", error);
    }
  }

  // Modified to take a directive parameter instead of using state
  async function handleSetDirective(directive: string) {
    try {
      const baseUrl =
        import.meta.env.VITE_SIM_BASE_URL ?? "http://localhost:8000";

      const response = await fetch(`${baseUrl}/set_directive`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ text: directive }),
      });

      const data = await response.json();
      console.log("Directive response:", data);

      if (data.status === "directive_enqueued") {
        alert(`New directive sent to robot: ${directive}`);
      }
    } catch (error) {
      console.error("Error setting directive:", error);
    }
  }

  if (!isAuthenticated && import.meta.env.VITE_REQUIRE_AUTH === "true") {
    return (
      <Container>
        <Title>Please Enter Password</Title>
        <AuthContainer>
          <StyledPasswordInput
            type="password"
            placeholder="Password"
            value={enteredPassword}
            onChange={(e) => setEnteredPassword(e.target.value)}
          />
          <StyledSubmitButton onClick={handlePasswordSubmit}>
            Submit
          </StyledSubmitButton>
        </AuthContainer>
      </Container>
    );
  }

  // If authenticated, show the simulator
  return (
    <Container className="App">
      <ResetButton onClick={handleResetRobot}>
        <MdRefresh size={20} /> Reset Robot
      </ResetButton>

      <DirectiveButtonsContainer>
        <DirectiveButton
          title="Default Mode"
          onClick={() => handleSetDirective("default_directive")}
        >
          <MdAutoMode size={20} />
        </DirectiveButton>
        <DirectiveButton
          title="Sassy Mode"
          onClick={() => handleSetDirective("sassy_directive")}
        >
          <MdMood size={20} />
        </DirectiveButton>
        <DirectiveButton
          title="Guide Mode"
          onClick={() => handleSetDirective("friendly_guide_directive")}
        >
          <MdTour size={20} />
        </DirectiveButton>
        <DirectiveButton
          title="Security Mode"
          onClick={() => handleSetDirective("security_patrol_directive")}
        >
          <MdSecurity size={20} />
        </DirectiveButton>
        <DirectiveButton
          title="Elder Care Mode"
          onClick={() => handleSetDirective("elder_safety_directive")}
        >
          <MdHealthAndSafety size={20} />
        </DirectiveButton>
      </DirectiveButtonsContainer>

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
