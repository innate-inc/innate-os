import { useState } from "react";
import styled from "styled-components";
import "./App.css";
import { ImageDisplay } from "./components/ImageDisplay";
import { ToggleViewMode } from "./components/ToggleViewMode";
import { Chat } from "./components/Chat";
import { MdRefresh } from "react-icons/md";
import { AuthGuard } from "./components/auth/AuthGuard";
import { UserProfile } from "./components/auth/UserProfile";
import { LogoutButton } from "./components/auth/LogoutButton";
import { useAuth0 } from "@auth0/auth0-react";

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

const UserContainer = styled.div`
  position: absolute;
  top: 10px;
  right: 10px;
  display: flex;
  align-items: center;
  gap: 10px;
`;

// The main application component
function SimulatorApp() {
  const [viewMode, setViewMode] = useState<
    "sideBySide" | "frontFocus" | "chaseFocus"
  >("sideBySide");

  const { isAuthenticated, getAccessTokenSilently } = useAuth0();

  async function handleResetRobot() {
    try {
      const baseUrl =
        import.meta.env.VITE_SIM_BASE_URL ?? "http://localhost:8000";

      // Get the access token if authenticated
      const headers: Record<string, string> = {};
      if (isAuthenticated) {
        try {
          const token = await getAccessTokenSilently();
          headers.Authorization = `Bearer ${token}`;
        } catch (error) {
          console.error("Error getting access token:", error);
        }
      }

      const response = await fetch(`${baseUrl}/reset_robot`, {
        method: "POST",
        headers,
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

  async function handleSetDirective(directive: string) {
    try {
      const baseUrl =
        import.meta.env.VITE_SIM_BASE_URL ?? "http://localhost:8000";

      // Get the access token if authenticated
      const headers: Record<string, string> = {
        "Content-Type": "application/json",
      };

      if (isAuthenticated) {
        try {
          const token = await getAccessTokenSilently();
          headers.Authorization = `Bearer ${token}`;
        } catch (error) {
          console.error("Error getting access token:", error);
        }
      }

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
      <ResetButton onClick={handleResetRobot}>
        <MdRefresh size={20} /> Reset Robot
      </ResetButton>

      {isAuthenticated && (
        <UserContainer>
          <UserProfile />
          <LogoutButton />
        </UserContainer>
      )}

      <TopSection>
        <Title>Innate Simulator</Title>
        <ImageDisplay viewMode={viewMode} />
        <ToggleViewMode viewMode={viewMode} setViewMode={setViewMode} />
      </TopSection>

      <ChatSection>
        <Chat onSetDirective={handleSetDirective} />
      </ChatSection>

      <VersionBadge>v0.1.0</VersionBadge>
    </Container>
  );
}

// Export the app wrapped with the AuthGuard
export default function App() {
  return (
    <AuthGuard>
      <SimulatorApp />
    </AuthGuard>
  );
}
