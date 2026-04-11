import { useState } from "react";
import { X } from "lucide-react";
import { api } from "../../api";

interface DatasetSummary {
  dir_name: string;
  name: string;
  episode_count: number;
}

interface DatasetDetail {
  dir_name: string;
  name: string;
  dataset_metadata: {
    episodes: {
      episode_id: number;
      file_name: string;
      start_timestamp: string;
    }[];
  };
}

interface Props {
  datasets: DatasetSummary[];
  onClose: () => void;
}

interface SourceSelection {
  skill_name: string;
  episode_ids: number[];
}

export default function MergeModal({ datasets, onClose }: Props) {
  const [newName, setNewName] = useState("");
  const [sources, setSources] = useState<SourceSelection[]>([]);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [episodeData, setEpisodeData] = useState<
    Record<string, DatasetDetail>
  >({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadEpisodes = async (dirName: string): Promise<DatasetDetail> => {
    const cached = episodeData[dirName];
    if (cached) return cached;
    const data = await api.get<DatasetDetail>(`/api/datasets/${dirName}`);
    setEpisodeData((prev) => ({ ...prev, [dirName]: data }));
    return data;
  };

  const toggleSource = async (dirName: string) => {
    const exists = sources.find((s) => s.skill_name === dirName);
    if (exists) {
      setSources(sources.filter((s) => s.skill_name !== dirName));
    } else {
      const detail = await loadEpisodes(dirName);
      const allIds = detail.dataset_metadata.episodes.map(
        (e) => e.episode_id,
      );
      setSources([...sources, { skill_name: dirName, episode_ids: allIds }]);
    }
  };

  const toggleEpisode = (dirName: string, epId: number) => {
    setSources(
      sources.map((s) => {
        if (s.skill_name !== dirName) return s;
        const has = s.episode_ids.includes(epId);
        return {
          ...s,
          episode_ids: has
            ? s.episode_ids.filter((id) => id !== epId)
            : [...s.episode_ids, epId],
        };
      }),
    );
  };

  const totalEpisodes = sources.reduce(
    (sum, s) => sum + s.episode_ids.length,
    0,
  );

  const handleMerge = async () => {
    if (!newName.trim() || sources.length === 0) return;
    setBusy(true);
    setError(null);
    try {
      await api.post("/api/datasets/merge", {
        new_name: newName.trim(),
        sources,
      });
      onClose();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-lg p-6 max-h-[80vh] flex flex-col">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-base font-bold">Merge Datasets</h3>
          <button
            onClick={onClose}
            className="text-innate-muted hover:text-black transition-colors"
          >
            <X size={18} />
          </button>
        </div>

        <label className="block text-[0.7rem] font-semibold text-innate-muted uppercase tracking-wide mb-1">
          New Dataset Name
        </label>
        <input
          type="text"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          placeholder="e.g. pick-socks-merged"
          className="w-full border border-innate-border rounded-md px-3 py-2 text-sm mb-4 focus:border-innate-purple focus:outline-none"
        />

        <p className="text-xs text-innate-muted uppercase font-semibold tracking-wide mb-2 border-b border-innate-border pb-1">
          Select Sources ({totalEpisodes} episodes selected)
        </p>

        <div className="flex-1 overflow-auto space-y-1 mb-4">
          {datasets.map((d) => {
            const isSource = sources.some(
              (s) => s.skill_name === d.dir_name,
            );
            const isExpanded = expanded === d.dir_name;
            return (
              <div
                key={d.dir_name}
                className="border border-innate-border rounded-lg"
              >
                <div className="flex items-center gap-2 px-3 py-2">
                  <input
                    type="checkbox"
                    checked={isSource}
                    onChange={() => toggleSource(d.dir_name)}
                    className="accent-innate-purple"
                  />
                  <button
                    className="flex-1 text-left text-sm font-medium hover:text-innate-purple transition-colors"
                    onClick={async () => {
                      setExpanded(isExpanded ? null : d.dir_name);
                      if (!isSource) await toggleSource(d.dir_name);
                      else await loadEpisodes(d.dir_name);
                    }}
                  >
                    {d.name}{" "}
                    <span className="text-innate-muted text-xs">
                      ({d.episode_count} ep)
                    </span>
                  </button>
                </div>
                {isExpanded && episodeData[d.dir_name] && (
                  <div className="px-3 pb-2">
                    <EpisodeChecklist
                      episodes={
                        episodeData[d.dir_name].dataset_metadata.episodes
                      }
                      selectedIds={
                        sources.find((s) => s.skill_name === d.dir_name)
                          ?.episode_ids || []
                      }
                      onToggle={(id) => toggleEpisode(d.dir_name, id)}
                    />
                  </div>
                )}
              </div>
            );
          })}
        </div>

        {error && (
          <p className="text-innate-orange text-xs mb-3">{error}</p>
        )}

        <div className="flex justify-end gap-2">
          <button
            onClick={onClose}
            className="px-4 py-1.5 rounded-full text-sm font-medium border border-innate-border hover:bg-black hover:text-white hover:border-black transition-all"
          >
            Cancel
          </button>
          <button
            onClick={handleMerge}
            disabled={
              !newName.trim() || totalEpisodes === 0 || busy
            }
            className="px-4 py-1.5 rounded-full text-sm font-semibold bg-innate-purple text-white hover:bg-innate-purple-hover transition-colors disabled:opacity-40"
          >
            {busy ? "Merging..." : `Merge ${totalEpisodes} Episodes`}
          </button>
        </div>
      </div>
    </div>
  );
}

function EpisodeChecklist({
  episodes,
  selectedIds,
  onToggle,
}: {
  episodes: { episode_id: number; file_name: string }[];
  selectedIds: number[];
  onToggle: (id: number) => void;
}) {
  return (
    <div className="grid grid-cols-2 gap-1 max-h-40 overflow-auto">
      {episodes.map((ep) => (
        <label
          key={ep.episode_id}
          className="flex items-center gap-1.5 text-xs text-gray-600 hover:text-black cursor-pointer"
        >
          <input
            type="checkbox"
            checked={selectedIds.includes(ep.episode_id)}
            onChange={() => onToggle(ep.episode_id)}
            className="accent-innate-purple"
          />
          <span className="font-mono">{ep.file_name}</span>
        </label>
      ))}
    </div>
  );
}
