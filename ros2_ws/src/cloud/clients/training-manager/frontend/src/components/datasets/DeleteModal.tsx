import { useState } from "react";
import { X } from "lucide-react";
import { api } from "../../api";

interface Props {
  skillName: string;
  episodeIds: number[];
  totalEpisodes: number;
  onClose: () => void;
}

export default function DeleteModal({
  skillName,
  episodeIds,
  totalEpisodes,
  onClose,
}: Props) {
  const [newName, setNewName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const kept = totalEpisodes - episodeIds.length;

  const handleCreate = async () => {
    if (!newName.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await api.post(`/api/datasets/${skillName}/copy`, {
        new_name: newName.trim(),
        excluded_episode_ids: episodeIds,
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
      <div className="bg-white rounded-xl shadow-xl w-full max-w-md p-6">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-base font-bold">Delete Episodes</h3>
          <button
            onClick={onClose}
            className="text-innate-muted hover:text-black transition-colors"
          >
            <X size={18} />
          </button>
        </div>

        <p className="text-sm text-gray-600 mb-1">
          Deleting {episodeIds.length} episode(s) from{" "}
          <span className="font-semibold">{skillName}</span>.
        </p>
        <p className="text-sm text-gray-600 mb-4">
          This creates a <strong>new</strong> dataset with the remaining{" "}
          {kept} episode(s). The original dataset is kept unchanged.
        </p>

        <label className="block text-[0.7rem] font-semibold text-innate-muted uppercase tracking-wide mb-1">
          New Dataset Name
        </label>
        <input
          type="text"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          placeholder="e.g. pick-socks-clean"
          className="w-full border border-innate-border rounded-md px-3 py-2 text-sm mb-4 focus:border-innate-purple focus:outline-none"
        />

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
            onClick={handleCreate}
            disabled={!newName.trim() || busy}
            className="px-4 py-1.5 rounded-full text-sm font-semibold bg-red-500 text-white hover:bg-red-600 transition-colors disabled:opacity-40"
          >
            {busy ? "Creating..." : "Create Without Selected"}
          </button>
        </div>
      </div>
    </div>
  );
}
