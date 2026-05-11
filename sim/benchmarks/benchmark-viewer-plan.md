# Benchmark Results Viewer Implementation Plan

## Project Setup
```bash
yarn create vite benchmark-viewer --template react-ts
cd benchmark-viewer
yarn add styled-components @types/styled-components
yarn add react-router-dom @types/react-router-dom
```

## Component Structure

### 1. App Layout
```typescript
// Basic layout with navigation and main content area
const AppLayout = styled.div`
  display: grid;
  grid-template-columns: 250px 1fr;
  min-height: 100vh;
`

const MainContent = styled.main`
  padding: 2rem;
  background: #f5f7fa;
`
```

### 2. BenchmarkList Component
```typescript
interface Benchmark {
  name: string;
  trials: Trial[];
  totalTrials: number;
  successCount: number;
}

const BenchmarkList: React.FC = () => {
  // Fetch and display list of benchmarks with success ratios
}
```

### 3. BenchmarkCard Component
```typescript
interface BenchmarkCardProps {
  benchmark: Benchmark;
}

const BenchmarkCard = styled.div`
  background: white;
  border-radius: 12px;
  padding: 1.5rem;
  box-shadow: 0 2px 4px rgba(0,0,0,0.1);
`

const SuccessRatio = styled.div<{ ratio: number }>`
  background: ${props => `linear-gradient(90deg, 
    #4CAF50 ${props.ratio}%, 
    #FF5252 ${props.ratio}%)`};
  height: 8px;
  border-radius: 4px;
`
```

### 4. TrialGrid Component
```typescript
interface Trial {
  id: string;
  success: boolean;
  reason: string;
  timestamp: string;
  metrics: {
    duration: number;
    chatMessages: number;
  };
}

const TrialGrid = styled.div`
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
  gap: 1rem;
`

const TrialCell = styled.div<{ success: boolean }>`
  background: ${props => props.success ? '#E8F5E9' : '#FFEBEE'};
  border: 1px solid ${props => props.success ? '#81C784' : '#E57373'};
  border-radius: 8px;
  padding: 1rem;
  cursor: pointer;
  transition: transform 0.2s;

  &:hover {
    transform: scale(1.05);
  }
`
```

### 5. TrialDetail Component
```typescript
interface TrialDetailProps {
  trial: Trial;
  onClose: () => void;
}

const TrialDetail = styled.div`
  position: fixed;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  background: white;
  padding: 2rem;
  border-radius: 12px;
  box-shadow: 0 4px 12px rgba(0,0,0,0.15);
  max-width: 800px;
  width: 90%;
`
```

### 6. Optional VideoPlayer Component
```typescript
interface VideoPlayerProps {
  firstPersonUrl: string;
  chaseUrl: string;
}

const VideoContainer = styled.div`
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1rem;
  margin-top: 1rem;
`
```

## Data Flow

1. Create API utilities for fetching benchmark data
```typescript
// src/api/benchmarks.ts
export const fetchBenchmarks = async (): Promise<Benchmark[]> => {
  // Fetch benchmark data from backend
}

export const fetchTrialDetails = async (id: string): Promise<Trial> => {
  // Fetch specific trial details
}
```

2. Implement context for global state
```typescript
// src/context/BenchmarkContext.tsx
interface BenchmarkContextType {
  benchmarks: Benchmark[];
  selectedTrial: Trial | null;
  setSelectedTrial: (trial: Trial | null) => void;
}
```

## Styling Theme

```typescript
// src/styles/theme.ts
export const theme = {
  colors: {
    success: '#4CAF50',
    error: '#FF5252',
    background: '#f5f7fa',
    surface: '#ffffff',
    text: '#2c3e50',
  },
  shadows: {
    card: '0 2px 4px rgba(0,0,0,0.1)',
    modal: '0 4px 12px rgba(0,0,0,0.15)',
  },
  borderRadius: {
    small: '4px',
    medium: '8px',
    large: '12px',
  },
}
```

## Implementation Order

1. Set up project with TypeScript and styled-components
2. Create basic layout and theme
3. Implement BenchmarkList and BenchmarkCard components
4. Add TrialGrid with success/failure visualization
5. Create TrialDetail modal for detailed view
6. Add optional video player component
7. Implement data fetching and state management
8. Add animations and polish UI