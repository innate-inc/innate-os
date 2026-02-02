import { useState, useEffect, useRef } from "react";
import styled from "styled-components";
import "./App.css";
import { ImageDisplay } from "./components/ImageDisplay";
import { Chat } from "./components/Chat";

// Main App Container
const AppContainer = styled.div`
  display: grid;
  grid-template-rows: auto 1fr auto;
  height: 100%;
  border: 1px solid ${({ theme }) => theme.colors.foreground};
  margin: 20px;
  max-width: 1600px;
  align-self: center;
  width: calc(100% - 40px);
`;

// Header
const Header = styled.header`
  display: grid;
  grid-template-columns: 250px 1fr auto;
  border-bottom: 1px solid ${({ theme }) => theme.colors.foreground};
  height: 60px;
  align-items: center;
`;

const Logo = styled.div`
  font-family: ${({ theme }) => theme.fonts.display};
  font-size: 24px;
  font-weight: 800;
  padding: 0 16px;
  letter-spacing: -0.02em;
  display: flex;
  align-items: center;
  height: 100%;
  border-right: 1px solid ${({ theme }) => theme.colors.foreground};
`;

const StatusBadge = styled.div`
  margin-right: 16px;
  background: ${({ theme }) => theme.colors.primary};
  color: white;
  padding: 6px 16px;
  border-radius: 20px;
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  display: flex;
  align-items: center;
  gap: 8px;
`;

const StatusDot = styled.div`
  width: 8px;
  height: 8px;
  background: #fff;
  border-radius: 50%;
  animation: pulse 2s infinite;
`;

// Workspace
const Workspace = styled.div`
  display: grid;
  grid-template-columns: 300px 1fr 450px;
  overflow: hidden;

  @media (max-width: 1200px) {
    grid-template-columns: 250px 1fr 390px;
  }

  @media (max-width: 1024px) {
    grid-template-columns: 200px 1fr;
  }
`;

// Left Sidebar
const Sidebar = styled.aside`
  border-right: 1px solid ${({ theme }) => theme.colors.foreground};
  display: flex;
  flex-direction: column;
  overflow-y: auto;
  background: ${({ theme }) => theme.colors.background};
`;

const PanelSection = styled.div`
  border-bottom: 1px solid ${({ theme }) => theme.colors.foreground};
`;

const PanelHeader = styled.div`
  padding: 12px 16px;
  font-size: 11px;
  text-transform: uppercase;
  border-bottom: 1px solid ${({ theme }) => theme.colors.foreground};
  font-weight: 700;
  opacity: 0.7;
`;

const BigStat = styled.div`
  padding: 16px;
`;

const StatValue = styled.div`
  font-family: ${({ theme }) => theme.fonts.display};
  font-size: 48px;
  line-height: 0.9;
  font-weight: 400;
  letter-spacing: -0.05em;
`;

const StatLabel = styled.div`
  font-size: 12px;
  margin-top: 8px;
  opacity: 0.6;
`;

const AgentItem = styled.div<{ $isActive: boolean }>`
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px;
  border-bottom: 1px solid ${({ theme }) => theme.colors.foreground};
  cursor: pointer;
  transition: background 0.2s;
  background: ${({ $isActive, theme }) =>
    $isActive ? theme.colors.foreground : "transparent"};
  color: ${({ $isActive, theme }) =>
    $isActive ? theme.colors.background : theme.colors.foreground};

  &:hover {
    background: ${({ $isActive, theme }) =>
      $isActive ? theme.colors.foreground : "rgba(255, 255, 255, 0.1)"};
  }
`;

const AgentName = styled.span`
  font-size: 13px;
  font-weight: 500;
`;

const AgentCheck = styled.div<{ $isActive: boolean }>`
  width: 16px;
  height: 16px;
  border: 1px solid
    ${({ $isActive, theme }) =>
      $isActive ? theme.colors.background : "currentColor"};
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  background: ${({ $isActive, theme }) =>
    $isActive ? theme.colors.primary : "transparent"};
  color: ${({ $isActive }) => ($isActive ? "white" : "transparent")};
  font-size: 10px;
`;

// Main Content - centers the 4:3 viewport
const MainContent = styled.main`
  display: flex;
  flex-direction: column;
  position: relative;
  background: #0a0a0a;
  overflow: hidden;
  align-items: center;
  justify-content: center;
`;

// Chat Column
const ChatColumn = styled.aside`
  border-left: 1px solid ${({ theme }) => theme.colors.foreground};
  display: flex;
  flex-direction: column;
  background: ${({ theme }) => theme.colors.background};
  overflow: hidden;

  @media (max-width: 1024px) {
    display: none;
  }
`;

// Footer
const Footer = styled.footer`
  border-top: 1px solid ${({ theme }) => theme.colors.foreground};
  height: 100px;
  display: grid;
  grid-template-columns: 1fr 350px;

  @media (max-width: 1024px) {
    grid-template-columns: 1fr;
  }
`;

const ControlPanel = styled.div`
  padding: 16px;
  display: flex;
  gap: 20px;
  align-items: center;
`;

const ActionButton = styled.button<{ $isDanger?: boolean }>`
  height: 50px;
  padding: 0 24px;
  font-family: ${({ theme }) => theme.fonts.mono};
  font-size: 12px;
  text-transform: uppercase;
  font-weight: 700;
  background: ${({ theme }) => theme.colors.background};
  color: ${({ $isDanger, theme }) =>
    $isDanger ? theme.colors.error : theme.colors.foreground};
  border: 1px solid
    ${({ $isDanger, theme }) =>
      $isDanger ? theme.colors.error : theme.colors.foreground};
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 10px;
  transition: all 0.1s;

  &:hover {
    background: ${({ $isDanger, theme }) =>
      $isDanger ? theme.colors.error : theme.colors.foreground};
    color: ${({ $isDanger, theme }) =>
      $isDanger ? "white" : theme.colors.background};
  }
`;

const WaveformViz = styled.div`
  border-left: 1px solid ${({ theme }) => theme.colors.foreground};
  display: flex;
  align-items: center;
  justify-content: center;
  flex-direction: column;
  padding: 10px;
  position: relative;

  @media (max-width: 1024px) {
    display: none;
  }
`;

const WaveBars = styled.div`
  display: flex;
  align-items: center;
  gap: 3px;
  height: 40px;
`;

const Bar = styled.div<{ $duration: number }>`
  width: 3px;
  background: ${({ theme }) => theme.colors.foreground};
  animation: wave ${({ $duration }) => $duration}s ease-in-out infinite;
`;

const VizLabel = styled.div`
  margin-top: 8px;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  opacity: 0.8;
`;

// Agent types
interface Agent {
  id: string;
  display_name: string;
  display_icon: string | null;
  prompt: string;
  skills: string[];
}

interface AvailableAgentsResponse {
  agents: Agent[];
  current_agent_id: string | null;
  startup_agent_id: string | null;
  error?: string;
}

export default function App() {
  const [activeAgent, setActiveAgent] = useState<string | null>(null);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [isLoadingAgents, setIsLoadingAgents] = useState(true);
  const hasAgentsRef = useRef(false);
  const [viewMode, setViewMode] = useState<
    "sideBySide" | "frontFocus" | "chaseFocus"
  >("frontFocus");

  // Fetch available agents from the robot on mount
  useEffect(() => {
    const fetchAgents = async () => {
      try {
        const baseUrl =
          import.meta.env.VITE_SIM_BASE_URL ?? "http://localhost:8000";
        const response = await fetch(`${baseUrl}/get_available_agents`);
        const data: AvailableAgentsResponse = await response.json();

        if (data.agents && data.agents.length > 0) {
          setAgents(data.agents);
          hasAgentsRef.current = true;
          // Set active agent to current agent from robot, or first agent
          if (data.current_agent_id) {
            setActiveAgent(data.current_agent_id);
          } else if (data.agents.length > 0) {
            setActiveAgent(data.agents[0].id);
          }
        }
      } catch (error) {
        console.error("Error fetching agents:", error);
      } finally {
        setIsLoadingAgents(false);
      }
    };

    fetchAgents();

    // Poll for agents every 5 seconds until we have some
    const intervalId = setInterval(async () => {
      if (!hasAgentsRef.current) {
        await fetchAgents();
      }
    }, 5000);

    return () => clearInterval(intervalId);
  }, []);

  async function handleResetRobot(memory_state?: string) {
    try {
      const baseUrl =
        import.meta.env.VITE_SIM_BASE_URL ?? "http://localhost:8000";

      const headers: Record<string, string> = {
        "Content-Type": "application/json",
      };

      const isValidMemoryState = typeof memory_state === "string";
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
    <AppContainer>
      <Header>
        <Logo>INNATE SIM</Logo>
        <div></div>
        <StatusBadge>
          <StatusDot />
          System Active
        </StatusBadge>
      </Header>

      <Workspace className="workspace">
        <Sidebar>
          <PanelSection>
            <PanelHeader>Unit Identifier</PanelHeader>
            <BigStat>
              <StatValue>MARS</StatValue>
              <StatLabel>Model Type: R7</StatLabel>
            </BigStat>
          </PanelSection>

          <PanelSection style={{ flex: 1 }}>
            <PanelHeader>Behavior Agents</PanelHeader>
            <div>
              {isLoadingAgents ? (
                <AgentItem $isActive={false}>
                  <AgentName>Loading agents...</AgentName>
                </AgentItem>
              ) : agents.length === 0 ? (
                <AgentItem $isActive={false}>
                  <AgentName>Waiting for robot connection</AgentName>
                </AgentItem>
              ) : (
                agents.map((agent) => (
                  <AgentItem
                    key={agent.id}
                    $isActive={agent.id === activeAgent}
                    onClick={() => {
                      setActiveAgent(agent.id);
                      handleSetDirective(agent.id);
                    }}
                  >
                    <AgentName>{agent.display_name}</AgentName>
                    <AgentCheck $isActive={agent.id === activeAgent}>
                      {agent.id === activeAgent && "✓"}
                    </AgentCheck>
                  </AgentItem>
                ))
              )}
            </div>
          </PanelSection>
        </Sidebar>

        <MainContent>
          <ImageDisplay
            viewMode={viewMode}
            setViewMode={setViewMode}
            onResetRobot={handleResetRobot}
            onSetDirective={handleSetDirective}
          />
        </MainContent>

        <ChatColumn className="col-chat">
          <PanelHeader>Interaction Log</PanelHeader>
          <Chat onSetDirective={handleSetDirective} />
        </ChatColumn>
      </Workspace>

      <Footer className="footer">
        <ControlPanel>
          <div style={{ flex: 1 }}></div>
          <ActionButton $isDanger onClick={() => handleResetRobot()}>
            Reset Systems
          </ActionButton>
        </ControlPanel>

        <WaveformViz>
          <WaveBars>
            <Bar $duration={0.5} />
            <Bar $duration={0.7} />
            <Bar $duration={0.4} />
            <Bar $duration={0.8} />
            <Bar $duration={0.6} />
            <Bar $duration={0.5} />
            <Bar $duration={0.7} />
            <Bar $duration={0.4} />
          </WaveBars>
          <VizLabel>Voice Input Active</VizLabel>
        </WaveformViz>
      </Footer>
    </AppContainer>
  );
}
