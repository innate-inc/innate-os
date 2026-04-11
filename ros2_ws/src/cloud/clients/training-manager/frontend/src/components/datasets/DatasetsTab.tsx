import { useEffect, useState } from "react";
import { Upload, Merge, RotateCcw } from "lucide-react";
import { api } from "../../api";
import EpisodeBrowser from "./EpisodeBrowser";
import MergeModal from "./MergeModal";

interface DatasetSummary {
  dir_name: string;
  name: string;
  type: string;
  episode_count: number;
  dataset_type: string;
  status: string;
  training_skill_id: string | null;
}

export default function DatasetsTab() {
  const [datasets, setDatasets] = useState<DatasetSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [mergeOpen, setMergeOpen] = useState(false);

  const refresh = () => {
    setLoading(true);
    setError(null);
    api
      .get<DatasetSummary[]>("/api/datasets")
      .then(setDatasets)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  };

  useEffect(refresh, []);

  if (selected) {
    return (
      <EpisodeBrowser
        skillName={selected}
        onBack={() => {
          setSelected(null);
          refresh();
        }}
      />
    );
  }

  const mergeable = datasets.filter(
    (d) => d.status === "uploaded" && d.dataset_type === "h264",
  );

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-xs font-semibold text-innate-muted uppercase tracking-wider border-b border-innate-border pb-2 flex-1">
          Datasets
        </h2>
        <div className="flex gap-2">
          {mergeable.length >= 2 && (
            <button
              onClick={() => setMergeOpen(true)}
              className="flex items-center gap-1.5 text-xs border border-innate-border rounded-full px-3 py-1 text-innate-muted hover:text-black hover:border-black transition-all"
            >
              <Merge size={13} /> Merge
            </button>
          )}
          <button
            onClick={refresh}
            className="text-xs text-innate-muted hover:text-black border border-innate-border rounded-full px-3 py-1 transition-all hover:border-black"
          >
            Refresh
          </button>
        </div>
      </div>

      {loading && (
        <p className="text-innate-muted text-sm">Loading datasets...</p>
      )}
      {error && (
        <p className="text-innate-orange text-sm">Error: {error}</p>
      )}

      {!loading && datasets.length === 0 && (
        <p className="text-innate-muted italic text-sm">
          No skills found in ~/skills/
        </p>
      )}

      {!loading && datasets.length > 0 && (
        <div className="space-y-2">
          {datasets.map((d) => (
            <DatasetRow
              key={d.dir_name}
              dataset={d}
              onSelect={() => setSelected(d.dir_name)}
              onRefresh={refresh}
            />
          ))}
        </div>
      )}

      {mergeOpen && (
        <MergeModal
          datasets={mergeable}
          onClose={() => {
            setMergeOpen(false);
            refresh();
          }}
        />
      )}
    </div>
  );
}

function DatasetRow({
  dataset,
  onSelect,
  onRefresh,
}: {
  dataset: DatasetSummary;
  onSelect: () => void;
  onRefresh: () => void;
}) {
  const [submitting, setSubmitting] = useState(false);
  const [progress, setProgress] = useState(0);
  const [stage, setStage] = useState("");

  const isUploaded = dataset.status === "uploaded";
  const isViewable = isUploaded && dataset.dataset_type === "h264";
  const isActive =
    dataset.status === "compressing" || dataset.status === "uploading";

  useEffect(() => {
    if (!isActive && !submitting) return;
    const interval = setInterval(async () => {
      try {
        const status = await api.get<{
          stage: string;
          done: boolean;
          progress: number;
          error: string | null;
        }>(`/api/datasets/${dataset.dir_name}/submit-status`);
        setStage(status.stage);
        setProgress(status.progress);
        if (status.done) {
          setSubmitting(false);
          clearInterval(interval);
          onRefresh();
        }
      } catch {
        /* ignore polling errors */
      }
    }, 1500);
    return () => clearInterval(interval);
  }, [isActive, submitting, dataset.dir_name, onRefresh]);

  const handleSubmit = async () => {
    setSubmitting(true);
    setProgress(0);
    setStage("compressing");
    try {
      await api.post(`/api/datasets/${dataset.dir_name}/submit`, {});
    } catch {
      setSubmitting(false);
    }
  };

  const showProgress = submitting || isActive;

  return (
    <div className="flex items-center border border-innate-border rounded-lg px-4 py-3 gap-4">
      <div className="flex-1 min-w-0">
        <button
          onClick={isViewable ? onSelect : undefined}
          disabled={!isViewable}
          className={`text-sm font-semibold text-left ${isViewable ? "hover:text-innate-purple cursor-pointer" : "text-gray-400 cursor-default"}`}
        >
          {dataset.name}
        </button>
        <div className="flex gap-3 text-xs text-innate-muted mt-0.5">
          <span>{dataset.episode_count} episodes</span>
          <span>{dataset.dataset_type}</span>
          <span className="font-mono text-gray-400">{dataset.dir_name}</span>
        </div>
      </div>

      <div className="shrink-0 flex items-center gap-2">
        {isUploaded && (
          <>
            <span className="inline-block px-2 py-0.5 rounded-full text-[0.68rem] font-semibold uppercase bg-green-100 text-green-700">
              uploaded
            </span>
            <button
              onClick={handleSubmit}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-semibold border border-innate-border text-innate-muted hover:text-black hover:border-black transition-all"
            >
              <RotateCcw size={13} /> Reupload
            </button>
          </>
        )}
        {!isUploaded && !showProgress && (
          <button
            onClick={handleSubmit}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-semibold bg-innate-purple text-white hover:bg-innate-purple-hover transition-colors"
          >
            <Upload size={13} /> Submit
          </button>
        )}
        {showProgress && (
          <div className="flex items-center gap-2">
            <ProgressRing progress={progress} size={32} />
            <span className="text-xs text-innate-muted capitalize">{stage}</span>
          </div>
        )}
      </div>
    </div>
  );
}

function ProgressRing({
  progress,
  size,
}: {
  progress: number;
  size: number;
}) {
  const stroke = 3;
  const radius = (size - stroke) / 2;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference - progress * circumference;

  return (
    <svg width={size} height={size} className="-rotate-90">
      <circle
        cx={size / 2}
        cy={size / 2}
        r={radius}
        fill="none"
        stroke="#f1eeee"
        strokeWidth={stroke}
      />
      <circle
        cx={size / 2}
        cy={size / 2}
        r={radius}
        fill="none"
        stroke="#401ffb"
        strokeWidth={stroke}
        strokeDasharray={circumference}
        strokeDashoffset={offset}
        strokeLinecap="round"
        className="transition-all duration-300"
      />
    </svg>
  );
}
