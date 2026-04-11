import { ReactNode, useState, useEffect, useRef } from "react";
import { Terminal, ChevronDown, ChevronUp } from "lucide-react";

type Tab = "skills" | "datasets" | "training";

interface Props {
  activeTab: Tab;
  onTabChange: (tab: Tab) => void;
  children: ReactNode;
}

const TABS: { id: Tab; label: string }[] = [
  { id: "skills", label: "Skills" },
  { id: "datasets", label: "Datasets" },
  { id: "training", label: "Training" },
];

interface LogEntry {
  timestamp: number;
  level: string;
  message: string;
}

export default function Layout({ activeTab, onTabChange, children }: Props) {
  const [terminalOpen, setTerminalOpen] = useState(true);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const logEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const es = new EventSource("/api/logs");
    es.onmessage = (e) => {
      const entry: LogEntry = JSON.parse(e.data);
      setLogs((prev) => [...prev.slice(-499), entry]);
    };
    return () => es.close();
  }, []);

  useEffect(() => {
    if (terminalOpen) {
      logEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [logs, terminalOpen]);

  const levelColor = (level: string) => {
    switch (level) {
      case "ERROR":
      case "CRITICAL":
        return "text-red-400";
      case "WARNING":
        return "text-orange-400";
      case "DEBUG":
        return "text-gray-500";
      default:
        return "text-gray-300";
    }
  };

  return (
    <div className="flex flex-col h-screen">
      {/* Header */}
      <header className="shrink-0 border-b border-innate-border px-6 py-4">
        <h1 className="text-xl font-bold tracking-tight mb-3">
          Innate Training Manager
        </h1>
        <nav className="flex gap-2">
          {TABS.map((t) => (
            <button
              key={t.id}
              onClick={() => onTabChange(t.id)}
              className={`px-4 py-1.5 rounded-full text-sm font-medium border transition-all ${
                activeTab === t.id
                  ? "bg-black text-white border-black"
                  : "bg-white text-innate-muted border-innate-border hover:border-gray-400 hover:text-black"
              }`}
            >
              {t.label}
            </button>
          ))}
        </nav>
      </header>

      {/* Main content */}
      <main className="flex-1 overflow-auto px-6 py-5">{children}</main>

      {/* Terminal panel */}
      <div className="shrink-0 border-t border-innate-border bg-gray-900">
        <button
          onClick={() => setTerminalOpen(!terminalOpen)}
          className="w-full flex items-center gap-2 px-4 py-1.5 text-xs font-mono text-gray-400 hover:text-gray-200 transition-colors"
        >
          <Terminal size={14} />
          <span>Logs</span>
          {logs.length > 0 && (
            <span className="text-gray-600">({logs.length})</span>
          )}
          <span className="ml-auto">
            {terminalOpen ? <ChevronDown size={14} /> : <ChevronUp size={14} />}
          </span>
        </button>
        {terminalOpen && (
          <div className="h-52 overflow-auto px-4 pb-2 font-mono text-xs leading-5">
            {logs.length === 0 && (
              <p className="text-gray-600 italic py-2">No log entries yet.</p>
            )}
            {logs.map((entry, i) => (
              <div key={i} className="flex gap-2">
                <span className="text-gray-600 shrink-0">
                  {new Date(entry.timestamp * 1000).toLocaleTimeString()}
                </span>
                <span
                  className={`shrink-0 w-12 text-right ${levelColor(entry.level)}`}
                >
                  {entry.level}
                </span>
                <span className="text-gray-300 break-all">{entry.message}</span>
              </div>
            ))}
            <div ref={logEndRef} />
          </div>
        )}
      </div>
    </div>
  );
}
