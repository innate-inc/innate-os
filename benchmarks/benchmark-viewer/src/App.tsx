import React, { useState, useEffect } from "react";
import { ThemeProvider } from "styled-components";
import { BrowserRouter as Router } from "react-router-dom";
import { theme } from "./styles/theme";
import { AppLayout, Sidebar, MainContent } from "./components/layout/AppLayout";
import { BenchmarkCard } from "./components/benchmark/BenchmarkCard";
import { TrialGrid } from "./components/trial/TrialGrid";
import GlobalStyle from "./styles/GlobalStyle";
import { Benchmark, Trial } from "./types/benchmark";
import styled from "styled-components";

const PageTitle = styled.h1`
  color: ${({ theme }) => theme.colors.text};
  margin-bottom: ${({ theme }) => theme.spacing.lg};
  font-size: 2rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
`;

const Description = styled.p`
  color: ${({ theme }) => theme.colors.textLight};
  margin-bottom: ${({ theme }) => theme.spacing.xl};
  line-height: 1.5;
`;

const TrialDetails = styled.div`
  background: ${({ theme }) => theme.colors.surface};
  border-radius: ${({ theme }) => theme.borderRadius.large};
  padding: ${({ theme }) => theme.spacing.xl};
  margin-top: ${({ theme }) => theme.spacing.xl};
  box-shadow: ${({ theme }) => theme.shadows.card};
  border-left: 4px solid
    ${({ theme, success }) =>
      success ? theme.colors.success : theme.colors.error};
`;

const TrialTitle = styled.h2`
  color: ${({ theme }) => theme.colors.text};
  margin-bottom: ${({ theme }) => theme.spacing.lg};
  font-size: 1.5rem;
`;

const MetricGrid = styled.div`
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: ${({ theme }) => theme.spacing.lg};
  margin-bottom: ${({ theme }) => theme.spacing.xl};
`;

const Metric = styled.div`
  background: ${({ theme }) => theme.colors.background};
  padding: ${({ theme }) => theme.spacing.lg};
  border-radius: ${({ theme }) => theme.borderRadius.medium};
`;

const MetricLabel = styled.div`
  color: ${({ theme }) => theme.colors.textLight};
  font-size: 0.875rem;
  margin-bottom: ${({ theme }) => theme.spacing.xs};
`;

const MetricValue = styled.div`
  color: ${({ theme }) => theme.colors.text};
  font-size: 1.25rem;
  font-weight: bold;
`;

const ChatLog = styled.div`
  margin-top: ${({ theme }) => theme.spacing.xl};
`;

const ChatMessage = styled.div`
  padding: ${({ theme }) => theme.spacing.md};
  border-bottom: 1px solid ${({ theme }) => theme.colors.border};

  &:last-child {
    border-bottom: none;
  }
`;

const ChatSender = styled.span`
  font-weight: bold;
  color: ${({ theme }) => theme.colors.text};
  margin-right: ${({ theme }) => theme.spacing.sm};
`;

const ChatTime = styled.span`
  color: ${({ theme }) => theme.colors.textLight};
  font-size: 0.875rem;
  margin-left: ${({ theme }) => theme.spacing.sm};
`;

const LoadingOverlay = styled.div`
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100vh;
  font-size: 1.2rem;
  color: ${({ theme }) => theme.colors.textLight};
`;

const StatusTag = styled.div<{ success: boolean }>`
  display: inline-block;
  padding: ${({ theme }) => `${theme.spacing.xs} ${theme.spacing.sm}`};
  border-radius: ${({ theme }) => theme.borderRadius.small};
  background: ${({ theme, success }) =>
    success ? theme.colors.success + "20" : theme.colors.error + "20"};
  color: ${({ theme, success }) =>
    success ? theme.colors.success : theme.colors.error};
  font-weight: bold;
  font-size: 0.875rem;
  margin-bottom: ${({ theme }) => theme.spacing.md};
`;

const loadBenchmarkData = async (benchmarkName: string): Promise<Benchmark> => {
  const trials: Trial[] = [];
  let successCount = 0;

  const trialNumbers = Array.from({ length: 10 }, (_, i) => i + 1);

  for (const trialNum of trialNumbers) {
    try {
      const [metadata, metrics, chatLog] = await Promise.all([
        fetch(`/results/${benchmarkName}/trial_${trialNum}/metadata.json`).then(
          (r) => r.json()
        ),
        fetch(`/results/${benchmarkName}/trial_${trialNum}/metrics.json`).then(
          (r) => r.json()
        ),
        fetch(`/results/${benchmarkName}/trial_${trialNum}/chat_log.json`).then(
          (r) => r.json()
        ),
      ]);

      const trial: Trial = {
        id: String(trialNum),
        success: metrics.success?.success || false,
        reason: metrics.success?.reason || "No reason provided",
        timestamp: metadata.timestamp,
        metrics: {
          duration:
            (new Date(metrics.end_time).getTime() -
              new Date(metrics.start_time).getTime()) /
            1000,
          chatMessages: metrics.chat_messages,
          frames_captured: metrics.frames_captured,
        },
        chat_log: chatLog,
      };

      if (trial.success) successCount++;
      trials.push(trial);
    } catch (error) {
      console.error(`Error loading trial ${trialNum}:`, error);
    }
  }

  return {
    name: benchmarkName,
    trials,
    totalTrials: trials.length,
    successCount,
    description: trials[0]?.metadata?.description || "",
    goal: trials[0]?.metadata?.goal || "",
  };
};

const App: React.FC = () => {
  const [benchmarks, setBenchmarks] = useState<Benchmark[]>([]);
  const [selectedBenchmark, setSelectedBenchmark] = useState<Benchmark | null>(
    null
  );
  const [selectedTrial, setSelectedTrial] = useState<Trial | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const loadData = async () => {
      try {
        // First get the list of benchmarks from index.json
        const indexResponse = await fetch("/results/index.json");
        const indexData = await indexResponse.json();
        const benchmarkNames = indexData.benchmarks;

        console.log("Loading benchmarks:", benchmarkNames);

        const loadedBenchmarks = await Promise.all(
          benchmarkNames.map((name) => loadBenchmarkData(name))
        );

        console.log("Loaded benchmarks:", loadedBenchmarks);

        setBenchmarks(loadedBenchmarks);
        if (loadedBenchmarks.length > 0) {
          setSelectedBenchmark(loadedBenchmarks[0]);
        }
        setLoading(false);
      } catch (error) {
        console.error("Error loading benchmarks:", error);
        setLoading(false);
      }
    };

    loadData();
  }, []);

  const handleBenchmarkClick = (benchmark: Benchmark) => {
    setSelectedBenchmark(benchmark);
    setSelectedTrial(null);
  };

  const handleTrialClick = (trial: Trial) => {
    setSelectedTrial(trial);
  };

  if (loading) {
    return (
      <ThemeProvider theme={theme}>
        <GlobalStyle />
        <LoadingOverlay>Loading benchmark data...</LoadingOverlay>
      </ThemeProvider>
    );
  }

  return (
    <ThemeProvider theme={theme}>
      <GlobalStyle />
      <Router>
        <AppLayout>
          <Sidebar>
            {benchmarks.map((benchmark) => (
              <BenchmarkCard
                key={benchmark.name}
                benchmark={benchmark}
                onClick={() => handleBenchmarkClick(benchmark)}
              />
            ))}
          </Sidebar>
          <MainContent>
            {selectedBenchmark && (
              <>
                <PageTitle>{selectedBenchmark.name}</PageTitle>
                {selectedBenchmark.description && (
                  <Description>{selectedBenchmark.description}</Description>
                )}
                <TrialGrid
                  trials={selectedBenchmark.trials}
                  onTrialClick={handleTrialClick}
                />
                {selectedTrial && (
                  <TrialDetails success={selectedTrial.success}>
                    <TrialTitle>Trial {selectedTrial.id} Details</TrialTitle>
                    <StatusTag success={selectedTrial.success}>
                      {selectedTrial.success ? "Success" : "Failed"}
                    </StatusTag>
                    <MetricGrid>
                      <Metric>
                        <MetricLabel>Duration</MetricLabel>
                        <MetricValue>
                          {selectedTrial.metrics.duration.toFixed(1)}s
                        </MetricValue>
                      </Metric>
                      <Metric>
                        <MetricLabel>Chat Messages</MetricLabel>
                        <MetricValue>
                          {selectedTrial.metrics.chatMessages}
                        </MetricValue>
                      </Metric>
                      <Metric>
                        <MetricLabel>First Person Frames</MetricLabel>
                        <MetricValue>
                          {selectedTrial.metrics.frames_captured.first_person}
                        </MetricValue>
                      </Metric>
                      <Metric>
                        <MetricLabel>Chase Frames</MetricLabel>
                        <MetricValue>
                          {selectedTrial.metrics.frames_captured.chase}
                        </MetricValue>
                      </Metric>
                    </MetricGrid>
                    <Description>{selectedTrial.reason}</Description>
                    <ChatLog>
                      <TrialTitle>Chat Log</TrialTitle>
                      {selectedTrial.chat_log.map((msg, i) => (
                        <ChatMessage key={i}>
                          <ChatSender>{msg.sender}:</ChatSender>
                          {msg.text}
                          {msg.time_since_start !== undefined && (
                            <ChatTime>
                              (at {msg.time_since_start.toFixed(1)}s)
                            </ChatTime>
                          )}
                        </ChatMessage>
                      ))}
                    </ChatLog>
                  </TrialDetails>
                )}
              </>
            )}
          </MainContent>
        </AppLayout>
      </Router>
    </ThemeProvider>
  );
};

export default App;
