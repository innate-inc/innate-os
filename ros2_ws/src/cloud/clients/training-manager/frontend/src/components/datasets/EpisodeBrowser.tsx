import { useEffect, useState } from "react";
import { ArrowLeft, Trash2 } from "lucide-react";
import { api } from "../../api";
import DeleteModal from "./DeleteModal";

interface Episode {
  episode_id: number;
  file_name: string;
  start_timestamp: string;
  end_timestamp: string;
  video_files?: string[];
  has_video: boolean;
}

interface DatasetDetail {
  dir_name: string;
  name: string;
  training_skill_id: string | null;
  status: string;
  dataset_metadata: {
    data_frequency: number;
    dataset_type: string;
    number_of_episodes: number;
    episodes: Episode[];
  };
}

interface Props {
  skillName: string;
  onBack: () => void;
}

export default function EpisodeBrowser({ skillName, onBack }: Props) {
  const [data, setData] = useState<DatasetDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [expanded, setExpanded] = useState<number | null>(null);
  const [deleteOpen, setDeleteOpen] = useState(false);

  useEffect(() => {
    setLoading(true);
    api
      .get<DatasetDetail>(`/api/datasets/${skillName}`)
      .then(setData)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [skillName]);

  if (loading)
    return <p className="text-innate-muted text-sm">Loading...</p>;
  if (error || !data)
    return <p className="text-innate-orange text-sm">Error: {error}</p>;

  const episodes = data.dataset_metadata.episodes;
  const allSelected =
    episodes.length > 0 && selected.size === episodes.length;

  const toggleAll = () => {
    if (allSelected) {
      setSelected(new Set());
    } else {
      setSelected(new Set(episodes.map((e) => e.episode_id)));
    }
  };

  const toggleOne = (id: number) => {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setSelected(next);
  };

  const duration = (start: string, end: string) => {
    if (!start || !end) return "—";
    const ms =
      new Date(end).getTime() - new Date(start).getTime();
    if (isNaN(ms)) return "—";
    return `${(ms / 1000).toFixed(1)}s`;
  };

  return (
    <div>
      <button
        onClick={onBack}
        className="flex items-center gap-1 text-sm text-innate-muted hover:text-black mb-4 transition-colors"
      >
        <ArrowLeft size={16} /> Back to datasets
      </button>

      <div className="flex items-center justify-between mb-4">
        <div>
          <h2 className="text-lg font-bold">{data.name}</h2>
          <p className="text-xs text-innate-muted mt-0.5">
            {data.dataset_metadata.number_of_episodes} episodes &middot;{" "}
            {data.dataset_metadata.dataset_type} &middot;{" "}
            {data.dataset_metadata.data_frequency} Hz
          </p>
        </div>
        {selected.size > 0 && selected.size < episodes.length && (
          <button
            onClick={() => setDeleteOpen(true)}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-semibold bg-red-500 text-white hover:bg-red-600 transition-colors"
          >
            <Trash2 size={13} /> Delete {selected.size} selected
          </button>
        )}
      </div>

      <table className="w-full text-sm border-collapse">
        <thead>
          <tr className="border-b border-innate-border">
            <th className="text-left p-2 w-8">
              <input
                type="checkbox"
                checked={allSelected}
                onChange={toggleAll}
                className="accent-innate-purple"
              />
            </th>
            <th className="text-left p-2 text-xs text-innate-muted font-semibold uppercase tracking-wide">
              ID
            </th>
            <th className="text-left p-2 text-xs text-innate-muted font-semibold uppercase tracking-wide">
              File
            </th>
            <th className="text-left p-2 text-xs text-innate-muted font-semibold uppercase tracking-wide">
              Start
            </th>
            <th className="text-left p-2 text-xs text-innate-muted font-semibold uppercase tracking-wide">
              Duration
            </th>
            <th className="text-left p-2 text-xs text-innate-muted font-semibold uppercase tracking-wide">
              Video
            </th>
          </tr>
        </thead>
        <tbody>
          {episodes.map((ep) => (
            <EpisodeRow
              key={ep.episode_id}
              episode={ep}
              skillName={skillName}
              checked={selected.has(ep.episode_id)}
              onToggle={() => toggleOne(ep.episode_id)}
              expanded={expanded === ep.episode_id}
              onExpand={() =>
                setExpanded(
                  expanded === ep.episode_id ? null : ep.episode_id,
                )
              }
              duration={duration(ep.start_timestamp, ep.end_timestamp)}
            />
          ))}
        </tbody>
      </table>

      {deleteOpen && (
        <DeleteModal
          skillName={skillName}
          episodeIds={Array.from(selected)}
          totalEpisodes={episodes.length}
          onClose={() => {
            setDeleteOpen(false);
            onBack();
          }}
        />
      )}
    </div>
  );
}

function EpisodeRow({
  episode,
  skillName,
  checked,
  onToggle,
  expanded,
  onExpand,
  duration,
}: {
  episode: Episode;
  skillName: string;
  checked: boolean;
  onToggle: () => void;
  expanded: boolean;
  onExpand: () => void;
  duration: string;
}) {
  return (
    <>
      <tr
        className="border-b border-innate-border/50 hover:bg-innate-panel cursor-pointer"
        onClick={onExpand}
      >
        <td className="p-2" onClick={(e) => e.stopPropagation()}>
          <input
            type="checkbox"
            checked={checked}
            onChange={onToggle}
            className="accent-innate-purple"
          />
        </td>
        <td className="p-2 font-mono text-xs">{episode.episode_id}</td>
        <td className="p-2 font-mono text-xs text-gray-600">
          {episode.file_name}
        </td>
        <td className="p-2 text-xs text-gray-600">
          {episode.start_timestamp || "—"}
        </td>
        <td className="p-2 text-xs">{duration}</td>
        <td className="p-2 text-xs">
          {episode.has_video ? (
            <span className="text-innate-purple font-medium">Available</span>
          ) : (
            <span className="text-innate-muted">—</span>
          )}
        </td>
      </tr>
      {expanded && episode.has_video && (
        <tr>
          <td colSpan={6} className="p-4 bg-innate-panel border-b border-innate-border">
            <div className="flex gap-4 flex-wrap">
              {(episode.video_files || []).map((vf, idx) => (
                <div key={vf} className="space-y-1">
                  <p className="text-xs text-innate-muted font-mono">{vf}</p>
                  <video
                    controls
                    className="rounded-md border border-innate-border max-w-md"
                    src={`/api/datasets/${skillName}/episodes/${episode.episode_id}/video?camera=${idx}`}
                  />
                </div>
              ))}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}
