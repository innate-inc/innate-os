export interface Trial {
  id: string;
  success: boolean;
  reason: string;
  timestamp: string;
  metrics: {
    duration: number;
    chatMessages: number;
    frames_captured: {
      first_person: number;
      chase: number;
    };
  };
  chat_log: Array<{
    timestamp: number;
    text: string;
    sender: string;
    time_since_start?: number;
  }>;
}

export interface Benchmark {
  name: string;
  trials: Trial[];
  totalTrials: number;
  successCount: number;
  description?: string;
  goal?: string;
}