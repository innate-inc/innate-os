import { useEffect, useState } from "react";
import { ArrowLeft, Play } from "lucide-react";
import { api } from "../../api";

interface Defaults {
  hyperparameters: Record<string, string>;
  architecture: Record<string, unknown>;
  infrastructure: Record<string, unknown>;
}

interface SkillOption {
  dir_name: string;
  name: string;
  training_skill_id: string | null;
}

interface Props {
  onBack: () => void;
}

export default function NewRunForm({ onBack }: Props) {
  const [defaults, setDefaults] = useState<Defaults | null>(null);
  const [skills, setSkills] = useState<SkillOption[]>([]);
  const [loading, setLoading] = useState(true);

  const [selectedSkill, setSelectedSkill] = useState("");
  const [preset, setPreset] = useState("act-default");
  const [repo, setRepo] = useState("");
  const [ref, setRef] = useState("");
  const [hyperparams, setHyperparams] = useState<Record<string, string>>({});
  const [infra, setInfra] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([
      api.get<Defaults>("/api/training/defaults"),
      api.get<SkillOption[]>("/api/datasets"),
    ])
      .then(([d, s]) => {
        setDefaults(d);
        setHyperparams({ ...d.hyperparameters });
        setInfra({
          gpu_type: String(d.infrastructure.gpu_type || "5090"),
          min_gpus: String(d.infrastructure.min_gpus || 4),
          max_gpus: String(d.infrastructure.max_gpus || 4),
          hours: String(d.infrastructure.hours || 5),
          budget: String(d.infrastructure.budget || 200),
        });
        const uploaded = s.filter(
          (sk: SkillOption) => sk.training_skill_id,
        );
        setSkills(uploaded);
        if (uploaded.length > 0) setSelectedSkill(uploaded[0].dir_name);
      })
      .finally(() => setLoading(false));
  }, []);

  const handleSubmit = async () => {
    if (!selectedSkill) return;
    setSubmitting(true);
    setError(null);
    setResult(null);

    const env: Record<string, string> = {};
    for (const [k, v] of Object.entries(hyperparams)) {
      if (defaults && v !== defaults.hyperparameters[k]) {
        env[k] = v;
      }
    }

    try {
      const res = await api.post<{ run_id: number; status: string }>(
        `/api/training/runs/${selectedSkill}`,
        {
          preset,
          repo: repo.trim() || undefined,
          ref: ref.trim() || undefined,
          env: Object.keys(env).length > 0 ? env : undefined,
          gpu_type:
            infra.gpu_type !== String(defaults?.infrastructure.gpu_type)
              ? infra.gpu_type
              : undefined,
          min_gpus:
            infra.min_gpus !== String(defaults?.infrastructure.min_gpus)
              ? Number(infra.min_gpus)
              : undefined,
          max_gpus:
            infra.max_gpus !== String(defaults?.infrastructure.max_gpus)
              ? Number(infra.max_gpus)
              : undefined,
          hours:
            infra.hours !== String(defaults?.infrastructure.hours)
              ? Number(infra.hours)
              : undefined,
          budget:
            infra.budget !== String(defaults?.infrastructure.budget)
              ? Number(infra.budget)
              : undefined,
        },
      );
      setResult(`Run #${res.run_id} created (${res.status})`);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  if (loading)
    return <p className="text-innate-muted text-sm">Loading defaults...</p>;

  return (
    <div>
      <button
        onClick={onBack}
        className="flex items-center gap-1 text-sm text-innate-muted hover:text-black mb-4 transition-colors"
      >
        <ArrowLeft size={16} /> Back to runs
      </button>

      <h2 className="text-lg font-bold mb-5">Create Training Run</h2>

      <div className="max-w-2xl space-y-6">
        {/* Skill selector */}
        <div>
          <label className="block text-[0.7rem] font-semibold text-innate-muted uppercase tracking-wide mb-1">
            Skill
          </label>
          {skills.length === 0 ? (
            <p className="text-innate-orange text-sm">
              No uploaded skills available. Submit a skill first.
            </p>
          ) : (
            <select
              value={selectedSkill}
              onChange={(e) => setSelectedSkill(e.target.value)}
              className="w-full border border-innate-border rounded-md px-3 py-2 text-sm focus:border-innate-purple focus:outline-none"
            >
              {skills.map((s) => (
                <option key={s.dir_name} value={s.dir_name}>
                  {s.name} ({s.dir_name})
                </option>
              ))}
            </select>
          )}
        </div>

        {/* Preset */}
        <div>
          <label className="block text-[0.7rem] font-semibold text-innate-muted uppercase tracking-wide mb-1">
            Preset
          </label>
          <select
            value={preset}
            onChange={(e) => setPreset(e.target.value)}
            className="w-full border border-innate-border rounded-md px-3 py-2 text-sm focus:border-innate-purple focus:outline-none"
          >
            <option value="act-default">act-default</option>
          </select>
        </div>

        {/* Repository */}
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="block text-[0.7rem] font-semibold text-innate-muted uppercase tracking-wide mb-1">
              Repository
            </label>
            <input
              type="text"
              value={repo}
              onChange={(e) => setRepo(e.target.value)}
              placeholder="owner/repo"
              className="w-full border border-innate-border rounded-md px-3 py-1.5 text-sm font-mono focus:border-innate-purple focus:outline-none"
            />
          </div>
          <div>
            <label className="block text-[0.7rem] font-semibold text-innate-muted uppercase tracking-wide mb-1">
              Ref
            </label>
            <input
              type="text"
              value={ref}
              onChange={(e) => setRef(e.target.value)}
              placeholder="branch / tag / commit"
              className="w-full border border-innate-border rounded-md px-3 py-1.5 text-sm font-mono focus:border-innate-purple focus:outline-none"
            />
          </div>
        </div>

        {/* Hyperparameters */}
        <section>
          <h3 className="text-xs font-semibold text-innate-muted uppercase tracking-wider border-b border-innate-border pb-1 mb-3">
            Hyperparameters
          </h3>
          <div className="grid grid-cols-2 gap-3">
            {Object.entries(hyperparams).map(([key, value]) => (
              <ParamField
                key={key}
                label={key}
                value={value}
                defaultValue={defaults?.hyperparameters[key]}
                onChange={(v) =>
                  setHyperparams({ ...hyperparams, [key]: v })
                }
              />
            ))}
          </div>
        </section>

        {/* Infrastructure */}
        <section>
          <h3 className="text-xs font-semibold text-innate-muted uppercase tracking-wider border-b border-innate-border pb-1 mb-3">
            Infrastructure
          </h3>
          <div className="grid grid-cols-2 gap-3">
            {Object.entries(infra).map(([key, value]) => (
              <ParamField
                key={key}
                label={key}
                value={value}
                onChange={(v) => setInfra({ ...infra, [key]: v })}
              />
            ))}
          </div>
        </section>

        {/* Architecture (read-only) */}
        {defaults && (
          <section>
            <h3 className="text-xs font-semibold text-innate-muted uppercase tracking-wider border-b border-innate-border pb-1 mb-3">
              Architecture{" "}
              <span className="text-gray-400 normal-case">(read-only)</span>
            </h3>
            <div className="bg-innate-panel border border-innate-border rounded-md p-3 space-y-1">
              {Object.entries(defaults.architecture).map(([k, v]) => (
                <div key={k} className="flex text-xs">
                  <span className="text-innate-muted w-40 shrink-0">{k}</span>
                  <span className="font-mono text-gray-600">
                    {String(v)}
                  </span>
                </div>
              ))}
            </div>
          </section>
        )}

        {/* Submit */}
        {error && (
          <p className="text-innate-orange text-sm">{error}</p>
        )}
        {result && (
          <p className="text-innate-purple text-sm font-medium">{result}</p>
        )}

        <div className="flex gap-2">
          <button
            onClick={handleSubmit}
            disabled={!selectedSkill || submitting}
            className="flex items-center gap-1.5 px-5 py-2 rounded-full text-sm font-semibold bg-innate-purple text-white hover:bg-innate-purple-hover transition-colors disabled:opacity-40"
          >
            <Play size={14} />
            {submitting ? "Creating..." : "Create Run"}
          </button>
          <button
            onClick={onBack}
            className="px-4 py-2 rounded-full text-sm font-medium border border-innate-border hover:bg-black hover:text-white hover:border-black transition-all"
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}

function ParamField({
  label,
  value,
  defaultValue,
  onChange,
}: {
  label: string;
  value: string;
  defaultValue?: string;
  onChange: (v: string) => void;
}) {
  const isModified = defaultValue !== undefined && value !== defaultValue;
  return (
    <div>
      <label className="block text-[0.7rem] font-semibold text-innate-muted uppercase tracking-wide mb-0.5">
        {label}
        {isModified && (
          <span className="text-innate-purple ml-1 normal-case">
            (modified)
          </span>
        )}
      </label>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={`w-full border rounded-md px-3 py-1.5 text-sm font-mono focus:outline-none transition-colors ${
          isModified
            ? "border-innate-purple bg-purple-50"
            : "border-innate-border"
        } focus:border-innate-purple`}
      />
    </div>
  );
}
