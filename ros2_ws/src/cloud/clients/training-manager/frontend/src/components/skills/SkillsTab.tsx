import { useEffect, useState } from "react";
import { api } from "../../api";
import SkillDetail from "./SkillDetail";

interface SkillSummary {
  dir_name: string;
  name: string;
  type: string;
  episode_count: number;
  has_checkpoint: boolean;
  training_skill_id: string | null;
  guidelines: string;
}

export default function SkillsTab() {
  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  const refresh = () => {
    setLoading(true);
    setError(null);
    api
      .get<SkillSummary[]>("/api/skills")
      .then(setSkills)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  };

  useEffect(refresh, []);

  if (loading) {
    return <p className="text-innate-muted text-sm">Loading skills...</p>;
  }
  if (error) {
    return <p className="text-innate-orange text-sm">Error: {error}</p>;
  }

  if (selected) {
    return (
      <SkillDetail
        skillName={selected}
        onBack={() => {
          setSelected(null);
          refresh();
        }}
      />
    );
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-xs font-semibold text-innate-muted uppercase tracking-wider border-b border-innate-border pb-2 flex-1">
          Skills
        </h2>
        <button
          onClick={refresh}
          className="text-xs text-innate-muted hover:text-black border border-innate-border rounded-full px-3 py-1 transition-all hover:border-black"
        >
          Refresh
        </button>
      </div>

      {skills.length === 0 ? (
        <p className="text-innate-muted italic text-sm">
          No skills found in ~/skills/
        </p>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {skills.map((s) => (
            <button
              key={s.dir_name}
              onClick={() => setSelected(s.dir_name)}
              className="text-left border border-innate-border rounded-lg p-4 hover:bg-innate-panel transition-colors"
            >
              <div className="flex items-start justify-between mb-2">
                <span className="font-semibold text-sm">{s.name}</span>
                <StatusBadge skill={s} />
              </div>
              <div className="text-xs text-innate-muted space-y-0.5">
                <p>
                  Type: <span className="text-gray-700">{s.type}</span>
                </p>
                <p>
                  Episodes: <span className="text-gray-700">{s.episode_count}</span>
                </p>
                {s.guidelines && (
                  <p className="truncate mt-1 text-gray-500">{s.guidelines}</p>
                )}
              </div>
              <p className="mt-2 font-mono text-[0.68rem] text-gray-400 truncate">
                {s.dir_name}
              </p>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function StatusBadge({ skill }: { skill: SkillSummary }) {
  if (skill.type === "replay") {
    return (
      <span className="inline-block px-2 py-0.5 rounded-full text-[0.68rem] font-semibold uppercase bg-innate-border text-gray-600">
        replay
      </span>
    );
  }
  if (skill.has_checkpoint) {
    return (
      <span className="inline-block px-2 py-0.5 rounded-full text-[0.68rem] font-semibold uppercase bg-innate-purple text-white">
        ready
      </span>
    );
  }
  return (
    <span className="inline-block px-2 py-0.5 rounded-full text-[0.68rem] font-semibold uppercase bg-innate-orange text-white">
      in training
    </span>
  );
}
