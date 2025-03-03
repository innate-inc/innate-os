/**
 * A Chat component replicating the style you requested.
 */
import { useState, useEffect, useRef } from "react";
import styled from "styled-components";
// Example icon from react-icons (feel free to use your own icon or an SVG):
import { IoSend, IoPerson, IoHardwareChip } from "react-icons/io5";
// Import directive icons
import {
  IoHappy,
  IoFlag,
  IoShield,
  IoHeart,
  IoSettings,
} from "react-icons/io5";
import { RobotGroupedBubble } from "./RobotGroupedBubble";
import { SystemMessageBubble } from "./SystemMessageBubble";
import { groupMessages, Message, DisplayMessage } from "../utils/groupMessages";
import { useAuth0 } from "@auth0/auth0-react";
import { isAuthorized, fetchAndStoreUserInfo } from "../services/authService";

const ChatContainer = styled.div`
  /* Default desktop width */
  width: 800px;
  height: 100%;
  display: flex;
  flex-direction: column;
  align-self: center;
  font-family: ${({ theme }) => theme.fonts.body};

  /* On screens below 768px, take the full width */
  @media (max-width: 768px) {
    width: 100%;
  }
`;

const MessagesWrapper = styled.div`
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
`;

interface MessageBubbleProps {
  $isUser: boolean;
}

const MessageBubble = styled.div<MessageBubbleProps>`
  max-width: 80%;
  background: ${({ $isUser, theme }) =>
    $isUser
      ? "linear-gradient(135deg, #2563eb 0%, #4f46e5 100%)"
      : theme.colors.secondary};
  color: ${({ $isUser, theme }) =>
    $isUser ? "#ffffff" : theme.colors.foreground};
  border: ${({ $isUser, theme }) =>
    $isUser ? "none" : `1px solid ${theme.colors.border}`};
  border-radius: 18px;
  border-bottom-left-radius: ${({ $isUser }) => ($isUser ? "18px" : "0")};
  border-bottom-right-radius: ${({ $isUser }) => ($isUser ? "0" : "18px")};
  padding: 12px 16px;
  margin-bottom: 8px;
  align-self: ${({ $isUser }) => ($isUser ? "flex-end" : "flex-start")};
  text-align: ${({ $isUser }) => ($isUser ? "right" : "left")};
  font-size: 15px;
  line-height: 1.5;
  box-shadow: ${({ theme }) => theme.shadows.small};

  @media (prefers-color-scheme: dark) {
    background: ${({ $isUser }) =>
      $isUser
        ? "linear-gradient(135deg, #2563eb 0%, #4f46e5 100%)"
        : "#1e293b"};
    color: ${({ $isUser }) => ($isUser ? "#ffffff" : "#e5e7eb")};
    border: ${({ $isUser }) => ($isUser ? "none" : "1px solid #374151")};
  }
`;

const MessageSender = styled.div<{ $isUser: boolean }>`
  font-size: 13px;
  font-weight: ${({ theme }) => theme.fontWeights.semibold};
  margin-bottom: 4px;
  color: ${({ $isUser, theme }) => ($isUser ? "#f0f4ff" : theme.colors.muted)};
  display: flex;
  align-items: center;
  gap: 6px;

  @media (prefers-color-scheme: dark) {
    color: ${({ $isUser }) => ($isUser ? "#f0f4ff" : "#9ca3af")};
  }
`;

const InputArea = styled.div`
  flex-shrink: 0;
  display: flex;
  align-items: center;
  padding: 12px 16px;
  background: ${({ theme }) => theme.colors.secondary};
  border-radius: 20px;
  margin: 12px;
  box-shadow: ${({ theme }) => theme.shadows.small};
`;

const TextInput = styled.input`
  flex: 1;
  border: none;
  border-radius: 16px;
  padding: 12px;
  outline: none;
  background: transparent;
  font-size: 15px;
  font-family: ${({ theme }) => theme.fonts.body};
  color: ${({ theme }) => theme.colors.foreground};
  ::placeholder {
    color: ${({ theme }) => theme.colors.muted};
  }
`;

const SendButton = styled.button`
  background: linear-gradient(135deg, #2563eb 0%, #4f46e5 100%);
  border: none;
  border-radius: 50%;
  width: 40px;
  height: 40px;
  display: flex;
  align-items: center;
  padding: 0;
  justify-content: center;
  cursor: pointer;
  margin-left: 8px;
  color: white;
  transition: transform 0.2s ease;

  &:hover {
    transform: scale(1.05);
  }
`;

const DirectivesContainer = styled.div`
  display: flex;
  overflow-x: auto;
  padding: 8px 12px;
  gap: 8px;
  background: ${({ theme }) => theme.colors.secondary};
  border-bottom: 1px solid ${({ theme }) => theme.colors.border};
`;

interface DirectiveButtonProps {
  $isActive: boolean;
}

const DirectiveButton = styled.button<DirectiveButtonProps>`
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-radius: 9999px;
  min-width: max-content;
  transition: all 0.2s ease;
  border: none;
  cursor: pointer;
  font-family: ${({ theme }) => theme.fonts.body};

  background: ${({ $isActive }) =>
    $isActive
      ? "linear-gradient(135deg, #2563eb 0%, #4f46e5 100%)"
      : "transparent"};
  color: ${({ $isActive, theme }) =>
    $isActive ? "#ffffff" : theme.colors.foreground};

  &:hover {
    background: ${({ $isActive, theme }) =>
      $isActive
        ? "linear-gradient(135deg, #2563eb 0%, #4f46e5 100%)"
        : theme.colors.background};
  }

  @media (prefers-color-scheme: dark) {
    color: ${({ $isActive }) => ($isActive ? "#ffffff" : "#e5e7eb")};

    &:hover {
      background: ${({ $isActive }) =>
        $isActive
          ? "linear-gradient(135deg, #2563eb 0%, #4f46e5 100%)"
          : "#334155"};
    }
  }
`;

const DirectiveContent = styled.div`
  display: flex;
  flex-direction: column;
  text-align: left;
`;

const DirectiveTitle = styled.span`
  font-weight: 500;
  font-size: 14px;
  display: block;
`;

const DirectiveSubtitle = styled.span`
  font-size: 12px;
  opacity: 0.8;
  display: block;
`;

const IconWrapper = styled.div<{ $isActive: boolean }>`
  position: relative;

  &::after {
    content: "";
    position: absolute;
    top: -2px;
    right: -2px;
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: white;
    display: ${({ $isActive }) => ($isActive ? "block" : "none")};
  }
`;

// Define the directive type
interface Directive {
  id: string;
  title: string;
  subtitle: string;
  icon: React.ReactNode;
}

// Define all available directives
const DIRECTIVES: Directive[] = [
  {
    id: "default_directive",
    title: "Default",
    subtitle: "Obedient, no initiative",
    icon: <IoSettings size={16} />,
  },
  {
    id: "sassy_directive",
    title: "Sassy",
    subtitle: "Playful, witty behavior",
    icon: <IoHappy size={16} />,
  },
  {
    id: "friendly_guide_directive",
    title: "Guide",
    subtitle: "Guides you around",
    icon: <IoFlag size={16} />,
  },
  {
    id: "security_patrol_directive",
    title: "Security",
    subtitle: "Vigilant, protective",
    icon: <IoShield size={16} />,
  },
  {
    id: "elder_safety_directive",
    title: "Elder Care",
    subtitle: "Proactively cares for users",
    icon: <IoHeart size={16} />,
  },
];

interface ChatProps {
  onSetDirective: (directive: string) => void;
}

export function Chat({ onSetDirective }: ChatProps) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [isScrolledToBottom, setIsScrolledToBottom] = useState(true);
  const wsRef = useRef<WebSocket | null>(null);
  const [activeDirective, setActiveDirective] = useState("default_directive");
  const [expandedSystemMessages, setExpandedSystemMessages] = useState<{
    [key: number]: boolean;
  }>({});
  const systemContentRefs = useRef<{ [key: number]: HTMLDivElement | null }>(
    {}
  );
  const { user, isAuthenticated, getAccessTokenSilently } = useAuth0();
  const [userInfoStored, setUserInfoStored] = useState(false);

  const handleScroll = () => {
    if (containerRef.current) {
      const { scrollTop, scrollHeight, clientHeight } = containerRef.current;
      setIsScrolledToBottom(scrollHeight - scrollTop - clientHeight < 10);
    }
  };

  // Effect to store user info when authenticated
  useEffect(() => {
    if (isAuthenticated && user && !userInfoStored) {
      const fetchUserInfo = async () => {
        try {
          const accessToken = await getAccessTokenSilently();
          const data = await fetchAndStoreUserInfo(user, accessToken);

          if (data) {
            // Check if the user is authorized
            if (data.is_authorized || data.email) {
              setUserInfoStored(true);
            } else {
              // If we don't have an email, try again after a delay
              setTimeout(() => {
                setUserInfoStored(false); // Reset to trigger another attempt
              }, 2000);
            }
          } else {
            // If we couldn't store the user info, try again after a delay
            setTimeout(() => {
              setUserInfoStored(false); // Reset to trigger another attempt
            }, 2000);
          }
        } catch (error) {
          // If there was an error, try again after a delay
          setTimeout(() => {
            setUserInfoStored(false); // Reset to trigger another attempt
          }, 2000);
        }
      };

      fetchUserInfo();
    }
  }, [isAuthenticated, user, userInfoStored, getAccessTokenSilently]);

  useEffect(() => {
    // If there's a socket and it's not fully closed, skip making a new one.
    if (
      wsRef.current &&
      wsRef.current.readyState !== WebSocket.CLOSED &&
      wsRef.current.readyState !== WebSocket.CLOSING
    ) {
      return;
    }

    // Check if the user is authorized to connect
    if (!isAuthorized(user)) {
      setMessages((prev) => {
        // Check if we already have this message to avoid duplicates
        const hasUnauthorizedMessage = prev.some(
          (m) =>
            m.sender === "system" &&
            m.text.includes("not authorized") &&
            m.isError
        );

        if (hasUnauthorizedMessage) {
          return prev;
        }

        return [
          ...prev,
          {
            sender: "system",
            text: "You are not authorized to use the chat. Please subscribe or contact axel@innate.bot for access.",
            timestamp: Date.now() / 1000,
            isError: true,
          },
        ];
      });
      return;
    }

    // Make sure user info is stored before connecting to WebSocket
    if (isAuthenticated && user && !userInfoStored) {
      return;
    }

    // Get user ID and email from Auth0 if authenticated
    const userId = isAuthenticated && user && user.sub ? user.sub : "anonymous";
    const userEmail = isAuthenticated && user && user.email ? user.email : "";

    // Add user ID and email as query parameters
    const wsUrl = `${
      import.meta.env.VITE_WS_BASE_URL
    }/ws/chat?user_id=${encodeURIComponent(userId)}&email=${encodeURIComponent(
      userEmail
    )}`;

    // Create a function to establish the WebSocket connection
    const connectWebSocket = () => {
      try {
        const socket = new WebSocket(wsUrl);
        wsRef.current = socket;

        socket.onopen = () => {
          console.log("Connected to chat websocket");
        };

        socket.onmessage = (event) => {
          try {
            const data = JSON.parse(event.data);

            // Handle error messages
            if (data.error) {
              setMessages((prev) => [
                ...prev,
                {
                  sender: data.sender || "system",
                  text: data.text,
                  timestamp: data.timestamp || Date.now() / 1000,
                  isError: true,
                },
              ]);
              return;
            }

            if (data.sender && data.text) {
              setMessages((prev) => {
                // Check if an identical message already exists
                const duplicateExists = prev.some(
                  (m) =>
                    m.sender === data.sender &&
                    m.text === data.text &&
                    m.timestamp === data.timestamp
                );
                if (duplicateExists) {
                  return prev;
                }
                // Add the new message and sort by timestamp in ascending order
                const newMessages = [
                  ...prev,
                  {
                    sender: data.sender,
                    text: data.text,
                    timestamp: data.timestamp,
                  },
                ];
                newMessages.sort((a, b) => a.timestamp - b.timestamp);
                return newMessages;
              });
            }
          } catch {
            console.error("Invalid message received:", event.data);
          }
        };

        socket.onclose = (event) => {
          // Try to reconnect after a delay if it wasn't a clean close
          if (!event.wasClean) {
            setTimeout(() => {
              if (wsRef.current?.readyState === WebSocket.CLOSED) {
                connectWebSocket();
              }
            }, 3000);
          }
        };

        socket.onerror = () => {
          console.error("WebSocket error");
        };

        return socket;
      } catch (error) {
        console.error("Error creating WebSocket connection:", error);
        return null;
      }
    };

    // Establish the initial connection
    const socket = connectWebSocket();

    // Cleanup function
    return () => {
      if (socket) {
        socket.close();
      }
    };
  }, [isAuthenticated, user, userInfoStored]);

  useEffect(() => {
    if (isScrolledToBottom) {
      messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages, isScrolledToBottom]);

  const handleSend = () => {
    const cleanDraft = draft.trim();
    if (!cleanDraft || !wsRef.current) return;

    // Check if the user is authorized to send messages
    if (!isAuthorized(user)) {
      setMessages((prev) => [
        ...prev,
        {
          sender: "system",
          text: "You are not authorized to send messages. Please subscribe or contact axel@innate.bot for access.",
          timestamp: Date.now() / 1000,
          isError: true,
        },
      ]);
      return;
    }

    // Check if WebSocket is open
    if (wsRef.current.readyState === WebSocket.OPEN) {
      // Send the draft message to the server via WebSocket
      wsRef.current.send(cleanDraft);

      // Add the message to the UI immediately
      setMessages((prev) => [
        ...prev,
        {
          sender: "user",
          text: cleanDraft,
          timestamp: Date.now() / 1000,
        },
      ]);

      // Clear the input
      setDraft("");
    } else {
      // Try to reconnect
      if (
        wsRef.current.readyState === WebSocket.CLOSED ||
        wsRef.current.readyState === WebSocket.CLOSING
      ) {
        // Force a re-render to trigger the useEffect that establishes the WebSocket connection
        setUserInfoStored(false);
      }

      // Add a message to the UI
      setMessages((prev) => [
        ...prev,
        {
          sender: "system",
          text: "You are not connected. Attempting to reconnect...",
          timestamp: Date.now() / 1000,
        },
      ]);
    }
  };

  // Use the grouping utility to prepare messages for display.
  const groupedMessages: DisplayMessage[] = groupMessages(messages);

  // Helper function to toggle system message expansion
  const toggleSystemMessage = (messageId: number) => {
    setExpandedSystemMessages((prev) => ({
      ...prev,
      [messageId]: !prev[messageId],
    }));
  };

  return (
    <ChatContainer>
      <DirectivesContainer>
        {DIRECTIVES.map((directive) => (
          <DirectiveButton
            key={directive.id}
            $isActive={activeDirective === directive.id}
            onClick={() => {
              setActiveDirective(directive.id);
              onSetDirective(directive.id);
            }}
          >
            <IconWrapper $isActive={activeDirective === directive.id}>
              {directive.icon}
            </IconWrapper>
            <DirectiveContent>
              <DirectiveTitle>{directive.title}</DirectiveTitle>
              <DirectiveSubtitle>{directive.subtitle}</DirectiveSubtitle>
            </DirectiveContent>
          </DirectiveButton>
        ))}
      </DirectivesContainer>

      <MessagesWrapper ref={containerRef} onScroll={handleScroll}>
        {groupedMessages.map((message, index) => {
          if (message.sender === "user") {
            return (
              <MessageBubble key={`${message.sender}-${index}`} $isUser>
                <MessageSender $isUser>
                  <span>You</span>
                  <IoPerson size={14} />
                </MessageSender>
                {message.text}
              </MessageBubble>
            );
          } else if (message.sender === "robot") {
            return (
              <MessageBubble key={`${message.sender}-${index}`} $isUser>
                <MessageSender $isUser>
                  <IoHardwareChip size={14} />
                  <span>Robot</span>
                </MessageSender>
                {message.text}
              </MessageBubble>
            );
          } else if (message.sender === "robot_grouped") {
            return (
              <RobotGroupedBubble
                key={`${message.sender}-${index}`}
                groupedExtras={message.groupedExtras}
                durationSeconds={message.durationSeconds}
                isLast={index === groupedMessages.length - 1}
              />
            );
          } else if (message.sender === "system") {
            return (
              <SystemMessageBubble
                key={`${message.sender}-${index}`}
                messageId={index}
                text={message.text}
                isExpanded={!!expandedSystemMessages[index]}
                onToggleExpand={toggleSystemMessage}
                contentRef={(el) => (systemContentRefs.current[index] = el)}
                isError={message.isError}
              />
            );
          }
          return null;
        })}
        {/* Marker element for auto-scroll */}
        <div ref={messagesEndRef} />
      </MessagesWrapper>
      <InputArea>
        <TextInput
          type="text"
          placeholder="Type your message..."
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              handleSend();
            }
          }}
        />
        <SendButton onClick={handleSend}>
          <IoSend size={20} style={{ display: "block", padding: 0 }} />
        </SendButton>
      </InputArea>
    </ChatContainer>
  );
}
