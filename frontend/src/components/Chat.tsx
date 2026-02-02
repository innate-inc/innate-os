/**
 * A Chat component replicating the style you requested.
 */
import { useState, useEffect, useRef } from "react";
import styled from "styled-components";
import { IoSend, IoPerson, IoHardwareChip, IoStop } from "react-icons/io5";
import { RobotGroupedBubble } from "./RobotGroupedBubble";
import { SystemMessageBubble } from "./SystemMessageBubble";
import { groupMessages, Message, DisplayMessage } from "../utils/groupMessages";
import Groq from "groq-sdk";
import { CartesiaClient } from "@cartesia/cartesia-js";

const ChatContainer = styled.div`
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  font-family: ${({ theme }) => theme.fonts.mono};
  background: ${({ theme }) => theme.colors.background};
`;

const MessagesWrapper = styled.div`
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 16px;
`;

interface MessageBubbleProps {
  $isUser: boolean;
}

const MessageBubble = styled.div<MessageBubbleProps>`
  max-width: 90%;
  padding: 8px 12px;
  align-self: ${({ $isUser }) => ($isUser ? "flex-end" : "flex-start")};
  text-align: ${({ $isUser }) => ($isUser ? "right" : "left")};
  font-size: 13px;
  line-height: 1.5;
  display: inline-block;
  background: ${({ $isUser, theme }) =>
    $isUser ? "rgba(64, 31, 251, 0.1)" : theme.colors.secondary};
  color: ${({ $isUser, theme }) =>
    $isUser ? theme.colors.primary : theme.colors.foreground};
  border: 1px solid
    ${({ $isUser, theme }) =>
      $isUser ? theme.colors.primary : theme.colors.foreground};
  border-bottom-left-radius: ${({ $isUser }) => ($isUser ? "4px" : "0")};
  border-bottom-right-radius: ${({ $isUser }) => ($isUser ? "0" : "4px")};
  box-shadow: ${({ $isUser }) =>
    $isUser ? "none" : "4px 4px 0 rgba(255,255,255,0.05)"};
`;

const MessageSender = styled.div<{ $isUser: boolean }>`
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  margin-bottom: 4px;
  opacity: 0.5;
  display: flex;
  align-items: center;
  gap: 6px;
`;

const InputArea = styled.div`
  flex-shrink: 0;
  display: flex;
  align-items: center;
  padding: 16px;
  gap: 8px;
  border-top: 1px solid ${({ theme }) => theme.colors.foreground};
`;

interface TextInputProps {
  $isListening: boolean;
}

const TextInput = styled.input<TextInputProps>`
  flex: 1;
  border: none;
  padding: 10px;
  outline: none;
  background: ${({ theme }) => theme.colors.secondary};
  font-size: 13px;
  font-family: ${({ theme }) => theme.fonts.mono};
  color: ${({ theme }) => theme.colors.foreground};
  border-bottom: 2px solid transparent;

  &:focus {
    border-bottom-color: ${({ theme }) => theme.colors.primary};
  }

  ::placeholder {
    color: ${({ theme }) => theme.colors.muted};
  }
`;

const SendButton = styled.button`
  width: 40px;
  height: 40px;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  color: ${({ theme }) => theme.colors.foreground};
  background: transparent;
  border: 1px solid ${({ theme }) => theme.colors.foreground};
  border-radius: 50%;
  transition: all 0.2s;

  &:hover {
    background: ${({ theme }) => theme.colors.foreground};
    color: ${({ theme }) => theme.colors.background};
  }
`;

const StopButton = styled.button`
  width: 40px;
  height: 40px;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  border-radius: 50%;
  transition: all 0.2s;
  border: 1px solid ${({ theme }) => theme.colors.foreground};
  background: transparent;
  color: ${({ theme }) => theme.colors.foreground};

  &:hover {
    background: #dc2626;
    border-color: #dc2626;
    color: white;
  }
`;

const DirectivesContainer = styled.div`
  display: flex;
  overflow-x: auto;
  padding: 8px 12px;
  gap: 8px;
  background: ${({ theme }) => theme.colors.background};
  border-bottom: 1px solid ${({ theme }) => theme.colors.foreground};
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

// Define the agent type (from robot)
interface Agent {
  id: string;
  display_name: string;
  display_icon: string | null;
  prompt: string;
  skills: string[];
}

// Available agents response from the API
interface AvailableAgentsResponse {
  agents: Agent[];
  current_agent_id: string | null;
  startup_agent_id: string | null;
  error?: string;
}

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
  const [activeDirective, setActiveDirective] = useState<string | null>(null);
  const [expandedSystemMessages, setExpandedSystemMessages] = useState<{
    [key: number]: boolean;
  }>({});
  const systemContentRefs = useRef<{ [key: number]: HTMLDivElement | null }>(
    {},
  );
  const [isListening, setIsListening] = useState(false);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const audioChunksRef = useRef<Blob[]>([]);
  const [isSpeaking, setIsSpeaking] = useState(false);
  const audioContextRef = useRef<AudioContext | null>(null);
  const cartesiaRef = useRef<CartesiaClient | null>(null);
  const websocketRef = useRef<any>(null);
  const audioBuffersRef = useRef<Float32Array[]>([]);
  const audioSourceRef = useRef<AudioBufferSourceNode | null>(null);
  const [audioQueue, setAudioQueue] = useState<Float32Array[]>([]);
  const isPlayingRef = useRef<boolean>(false);

  // State for agents fetched from the robot
  const [agents, setAgents] = useState<Agent[]>([]);
  const [isLoadingAgents, setIsLoadingAgents] = useState(true);
  const hasAgentsRef = useRef(false);

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
          // Set active directive to current agent from robot, or first agent
          if (data.current_agent_id) {
            setActiveDirective(data.current_agent_id);
          } else if (data.agents.length > 0) {
            setActiveDirective(data.agents[0].id);
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

  const handleScroll = () => {
    if (containerRef.current) {
      const { scrollTop, scrollHeight, clientHeight } = containerRef.current;
      setIsScrolledToBottom(scrollHeight - scrollTop - clientHeight < 10);
    }
  };

  useEffect(() => {
    // If there's a socket and it's not fully closed, skip making a new one.
    if (
      wsRef.current &&
      wsRef.current.readyState !== WebSocket.CLOSED &&
      wsRef.current.readyState !== WebSocket.CLOSING
    ) {
      return;
    }

    // Use anonymous user ID
    const userId = "anonymous";

    // Add user ID as query parameter
    const wsUrl = `${
      import.meta.env.VITE_WS_BASE_URL
    }/ws/chat?user_id=${encodeURIComponent(userId)}`;

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
                    m.timestamp === data.timestamp,
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
  }, []);

  useEffect(() => {
    if (isScrolledToBottom) {
      messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages, isScrolledToBottom]);

  // Listen for voice transcription events from App.tsx
  useEffect(() => {
    const handleVoiceTranscription = (event: CustomEvent<{ text: string }>) => {
      const text = event.detail.text;
      if (
        text &&
        wsRef.current &&
        wsRef.current.readyState === WebSocket.OPEN
      ) {
        wsRef.current.send(text);
        console.log("Sent voice transcription to chat:", text);
      }
    };

    window.addEventListener(
      "voice-transcription",
      handleVoiceTranscription as EventListener,
    );
    return () => {
      window.removeEventListener(
        "voice-transcription",
        handleVoiceTranscription as EventListener,
      );
    };
  }, []);

  const handleSend = async () => {
    const cleanDraft = draft.trim();
    if (!cleanDraft || !wsRef.current) return;

    // Initialize AudioContext on user interaction
    await ensureAudioContext();

    // Check if WebSocket is open
    if (wsRef.current.readyState === WebSocket.OPEN) {
      // Send the draft message to the server via WebSocket
      wsRef.current.send(cleanDraft);

      // Clear the input
      setDraft("");
    } else {
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

  // Speech recognition functions
  const startListening = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });

      mediaRecorderRef.current = new MediaRecorder(stream);
      audioChunksRef.current = [];

      mediaRecorderRef.current.ondataavailable = (event) => {
        if (event.data.size > 0) {
          audioChunksRef.current.push(event.data);
        }
      };

      mediaRecorderRef.current.onstop = async () => {
        const audioBlob = new Blob(audioChunksRef.current, {
          type: "audio/webm",
        });
        await processAudio(audioBlob);

        // Stop all tracks to release the microphone
        stream.getTracks().forEach((track) => track.stop());
      };

      mediaRecorderRef.current.start();
      setIsListening(true);
    } catch (error) {
      console.error("Error accessing microphone:", error);
      setMessages((prev) => [
        ...prev,
        {
          sender: "system",
          text: "Error accessing microphone. Please check your browser permissions.",
          timestamp: Date.now() / 1000,
          isError: true,
        },
      ]);
    }
  };

  const stopListening = () => {
    if (
      mediaRecorderRef.current &&
      mediaRecorderRef.current.state !== "inactive"
    ) {
      mediaRecorderRef.current.stop();
    }
    setIsListening(false);
  };

  const toggleListening = () => {
    if (isListening) {
      stopListening();
    } else {
      startListening();
    }
  };

  const stopAgent = async () => {
    try {
      const baseUrl =
        import.meta.env.VITE_SIM_BASE_URL ?? "http://localhost:8000";
      const response = await fetch(`${baseUrl}/stop_agent`, {
        method: "POST",
      });
      const data = await response.json();
      console.log("Agent stopped:", data);
      setMessages((prev) => [
        ...prev,
        {
          sender: "system",
          text: "Agent stopped.",
          timestamp: Date.now() / 1000,
        },
      ]);
    } catch (error) {
      console.error("Error stopping agent:", error);
      setMessages((prev) => [
        ...prev,
        {
          sender: "system",
          text: "Error stopping agent.",
          timestamp: Date.now() / 1000,
          isError: true,
        },
      ]);
    }
  };

  const processAudio = async (audioBlob: Blob) => {
    try {
      // Convert blob to base64
      const reader = new FileReader();
      reader.readAsDataURL(audioBlob);

      reader.onloadend = async () => {
        // Show processing message
        setMessages((prev) => [
          ...prev,
          {
            sender: "system",
            text: "Processing your speech...",
            timestamp: Date.now() / 1000,
          },
        ]);

        try {
          // Create a temporary file from the blob
          const file = new File([audioBlob], "recording.webm", {
            type: "audio/webm",
          });

          // Create FormData to send the file
          const formData = new FormData();
          formData.append("file", file);
          formData.append("model", "whisper-large-v3-turbo");

          console.log("Key", import.meta.env.VITE_GROQ_API_KEY);

          // Send to backend for processing with Groq API
          const groq = new Groq({
            apiKey: import.meta.env.VITE_GROQ_API_KEY || "",
            dangerouslyAllowBrowser: true,
          });
          const transcription = await groq.audio.transcriptions.create({
            file: file,
            model: "whisper-large-v3-turbo",
          });

          if (transcription && transcription.text) {
            // Set the transcribed text as the draft
            setDraft(transcription.text);

            // Remove the processing message
            setMessages((prev) =>
              prev.filter(
                (msg) =>
                  !(
                    msg.sender === "system" &&
                    msg.text === "Processing your speech..."
                  ),
              ),
            );
          } else {
            throw new Error("No transcription returned");
          }
        } catch (error) {
          console.error("Error processing audio:", error);

          // Remove the processing message and show error
          setMessages((prev) => {
            const filteredMessages = prev.filter(
              (msg) =>
                !(
                  msg.sender === "system" &&
                  msg.text === "Processing your speech..."
                ),
            );

            return [
              ...filteredMessages,
              {
                sender: "system",
                text: "Error processing your speech. Please try again.",
                timestamp: Date.now() / 1000,
                isError: true,
              },
            ];
          });
        }
      };
    } catch (error) {
      console.error("Error processing audio:", error);
      setMessages((prev) => [
        ...prev,
        {
          sender: "system",
          text: "Error processing your speech. Please try again.",
          timestamp: Date.now() / 1000,
          isError: true,
        },
      ]);
    }
  };

  const filteredMessages = messages.filter(
    (msg) => msg.sender !== "vision_agent_output",
  );

  // Use the grouping utility to prepare messages for display.
  const groupedMessages: DisplayMessage[] = groupMessages(filteredMessages);

  // Helper function to toggle system message expansion
  const toggleSystemMessage = (messageId: number) => {
    setExpandedSystemMessages((prev) => ({
      ...prev,
      [messageId]: !prev[messageId],
    }));
  };

  // Initialize Cartesia client
  useEffect(() => {
    if (!cartesiaRef.current) {
      cartesiaRef.current = new CartesiaClient({
        apiKey: import.meta.env.VITE_CARTESIA_API_KEY || "",
      });
    }

    // Don't create AudioContext here automatically
    // We'll create it on first user interaction instead

    return () => {
      // Clean up WebSocket connection if active
      if (websocketRef.current) {
        websocketRef.current.disconnect();
      }

      // Stop any playing audio
      if (audioSourceRef.current) {
        try {
          audioSourceRef.current.stop();
        } catch (e) {
          // Ignore errors if already stopped
        }
      }
    };
  }, []);

  // Function to ensure AudioContext is created and resumed
  const ensureAudioContext = async () => {
    // Create AudioContext if it doesn't exist
    if (!audioContextRef.current) {
      audioContextRef.current = new (
        window.AudioContext || (window as any).webkitAudioContext
      )();
    }

    // Resume the AudioContext if it's suspended
    if (audioContextRef.current.state === "suspended") {
      await audioContextRef.current.resume();
    }

    return audioContextRef.current.state === "running";
  };

  // Function to decode base64 to array buffer
  const base64ToArrayBuffer = (base64: string) => {
    const binaryString = atob(base64);
    const len = binaryString.length;
    const bytes = new Uint8Array(len);
    for (let i = 0; i < len; i++) {
      bytes[i] = binaryString.charCodeAt(i);
    }
    return bytes.buffer;
  };

  // Function to speak messages using Cartesia WebSocket
  const speakMessage = async (text: string, isUser: boolean = false) => {
    if (!cartesiaRef.current || isSpeaking || !text.trim()) return;

    try {
      // Don't try to auto-initialize AudioContext for messages that arrive automatically
      // Only proceed if we already have a running AudioContext
      if (
        !audioContextRef.current ||
        audioContextRef.current.state !== "running"
      ) {
        console.log("AudioContext not running, can't autoplay speech");
        return;
      }

      setIsSpeaking(true);
      // Clear any existing audio queue
      setAudioQueue([]);
      audioBuffersRef.current = [];
      isPlayingRef.current = false;

      // Initialize WebSocket if not already done
      if (!websocketRef.current) {
        websocketRef.current = cartesiaRef.current.tts.websocket({
          container: "raw",
          encoding: "pcm_f32le",
          sampleRate: 44100,
        });

        try {
          await websocketRef.current.connect();
        } catch (error) {
          console.error(`Failed to connect to Cartesia: ${error}`);
          setIsSpeaking(false);
          return;
        }
      }

      console.log(`Speaking ${isUser ? "user" : "robot"} message: "${text}"`);

      // Create a stream - use different voices for user vs robot
      const response = await websocketRef.current.send({
        modelId: "sonic-english",
        voice: {
          mode: "id",
          // Use different voice IDs for user vs robot for easy distinction
          id: isUser
            ? "a0e99841-438c-4a64-b679-ae501e7d6091" // User voice
            : "a0e99841-438c-4a64-b679-ae501e7d6091", // Robot voice (different ID)
        },
        transcript: text,
      });

      // Process audio data
      response.on("message", (message: any) => {
        // Handle chunked audio data
        // Parse the message
        const parsedMessage = JSON.parse(message);

        if (parsedMessage.type === "error") {
          console.error("Error during speech synthesis:", parsedMessage);
        }

        if (
          parsedMessage.type === "chunk" &&
          parsedMessage.data &&
          audioContextRef.current
        ) {
          try {
            // Convert base64 string to binary data
            const audioBuffer = base64ToArrayBuffer(parsedMessage.data);

            // Convert to Float32Array for Web Audio API
            const floatArray = new Float32Array(audioBuffer);

            // Add to the queue of chunks to play
            setAudioQueue((prevQueue) => [...prevQueue, floatArray]);
          } catch (error) {
            console.error("Error processing audio chunk:", error);
          }
        }

        // Handle completion
        if (parsedMessage.type === "chunk" && parsedMessage.done) {
          console.log(
            `Finished receiving ${isUser ? "user" : "robot"} message audio`,
          );
        }
      });

      // Handle errors
      response.on("error", (error: any) => {
        console.error("Error during speech synthesis:", error);
        setIsSpeaking(false);
      });
    } catch (error) {
      console.error("Error generating speech:", error);
      setIsSpeaking(false);
    }
  };

  // Effect to monitor the audio queue and play chunks sequentially
  useEffect(() => {
    const playNextChunk = async () => {
      if (
        !audioContextRef.current ||
        audioQueue.length === 0 ||
        isPlayingRef.current
      ) {
        return;
      }

      // Mark as playing
      isPlayingRef.current = true;

      // Get the next chunk
      const nextChunk = audioQueue[0];

      try {
        // Create an audio buffer
        const audioBuffer = audioContextRef.current.createBuffer(
          1, // mono
          nextChunk.length,
          44100, // sample rate
        );

        // Fill the buffer with our audio data
        audioBuffer.getChannelData(0).set(nextChunk);

        // Create a buffer source
        const source = audioContextRef.current.createBufferSource();
        audioSourceRef.current = source;
        source.buffer = audioBuffer;

        // When playback ends
        source.onended = () => {
          // Remove the played chunk from the queue
          setAudioQueue((prevQueue) => prevQueue.slice(1));
          audioSourceRef.current = null;
          isPlayingRef.current = false;

          // If this was the last chunk, we're done speaking
          if (audioQueue.length <= 1) {
            setIsSpeaking(false);
          }
        };

        // Connect to the audio context destination and play
        source.connect(audioContextRef.current.destination);
        source.start();
      } catch (error) {
        console.error("Error playing audio chunk:", error);
        isPlayingRef.current = false;
        setAudioQueue((prevQueue) => prevQueue.slice(1));

        // If this was the last chunk, we're done speaking
        if (audioQueue.length <= 1) {
          setIsSpeaking(false);
        }
      }
    };

    // Try to play the next chunk whenever the queue changes
    playNextChunk();
  }, [audioQueue]);

  // Stop speaking function - update to clear the queue
  const stopSpeaking = () => {
    if (audioSourceRef.current) {
      try {
        audioSourceRef.current.stop();
      } catch (e) {
        // Ignore errors if already stopped
      }
      audioSourceRef.current = null;
    }
    setAudioQueue([]); // Clear the queue
    isPlayingRef.current = false;
    setIsSpeaking(false);
  };

  // Modify the effect that handles new messages
  useEffect(() => {
    if (messages.length > 0) {
      const latestMessage = messages[messages.length - 1];

      // Don't try to auto-speak messages anymore
      // If the latest message is a robot message, speak it
      if (latestMessage.sender === "robot") {
        console.log("Speaking robot message");
        speakMessage(latestMessage.text, false);
      }

      // Scroll to bottom if needed
      if (isScrolledToBottom) {
        messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
      }
    }
  }, [messages]);

  // Add a button to enable audio for the session
  const enableAudio = async () => {
    try {
      const success = await ensureAudioContext();
      if (success) {
        // Maybe show a toast or some UI indication that audio is now enabled
        console.log("Audio enabled successfully");
      } else {
        console.warn("Failed to enable audio");
      }
    } catch (error) {
      console.error("Error enabling audio:", error);
    }
  };

  return (
    <ChatContainer>
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
              <MessageBubble key={`${message.sender}-${index}`} $isUser={false}>
                <MessageSender $isUser={false}>
                  <IoHardwareChip size={14} />
                  <span>Robot</span>
                  <button
                    onClick={async () => {
                      // This is a user interaction, so we can safely try to initialize AudioContext
                      await ensureAudioContext();
                      speakMessage(message.text, false);
                    }}
                    style={{
                      background: "transparent",
                      border: "none",
                      cursor: "pointer",
                      marginLeft: "auto",
                      opacity: isSpeaking ? 0.5 : 1,
                    }}
                    disabled={isSpeaking}
                    title={isSpeaking ? "Speaking..." : "Speak this message"}
                  >
                    🔊
                  </button>
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

      {/* Add a stop speaking button when active */}
      {isSpeaking && (
        <div
          style={{
            position: "fixed",
            bottom: "80px",
            right: "20px",
            zIndex: 100,
          }}
        >
          <button
            onClick={stopSpeaking}
            style={{
              background: "rgba(239, 68, 68, 0.9)",
              color: "white",
              border: "none",
              borderRadius: "50%",
              width: "40px",
              height: "40px",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              cursor: "pointer",
              boxShadow: "0 2px 10px rgba(0,0,0,0.2)",
            }}
          >
            🔇
          </button>
        </div>
      )}

      <InputArea>
        <StopButton onClick={stopAgent} title="Stop current agent action">
          <IoStop size={20} />
        </StopButton>
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
          $isListening={isListening}
          disabled={isListening}
        />
        <SendButton onClick={handleSend}>
          <IoSend size={20} style={{ display: "block", padding: 0 }} />
        </SendButton>
      </InputArea>
    </ChatContainer>
  );
}
