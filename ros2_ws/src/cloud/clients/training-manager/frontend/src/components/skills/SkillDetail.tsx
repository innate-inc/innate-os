import { useEffect, useState } from "react";
import { ArrowLeft, Save } from "lucide-react";
import { api } from "../../api";

interface SkillData {
  dir_name: string;
  metadata: Record<string, unknown>;
  dataset_metadata: Record<string, unknown> | null;
}

interface Props {
  skillName: string;
  onBack: () => void;
}

export default function SkillDetail({ skillName, onBack }: Props) {
  const [data, setData] = useState<SkillData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [edits, setEdits] = useState<Record<string, unknown>>({});
  const [executionJson, setExecutionJson] = useState("");
  const [jsonError, setJsonError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    api
      .get<SkillData>(`/api/skills/${skillName}`)
      .then((d) => {
        setData(d);
        setEdits({
          name: d.metadata.name as string,
          guidelines: d.metadata.guidelines as string,
          guidelines_when_running: d.metadata.guidelines_when_running as string,
        });
        setExecutionJson(
          JSON.stringify(d.metadata.execution || {}, null, 2),
        );
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [skillName]);

  const handleSave = async () => {
    setSaving(true);
    setSaveMsg(null);
    try {
      let execution: Record<string, unknown> | undefined;
      if (executionJson.trim()) {
        try {
          execution = JSON.parse(executionJson);
        } catch {
          setSaveMsg("Error: invalid execution JSON");
          setSaving(false);
          return;
        }
      }
      await api.put(`/api/skills/${skillName}`, { ...edits, execution });
      setSaveMsg("Saved!");
      setTimeout(() => setSaveMsg(null), 2000);
    } catch (e: unknown) {
      setSaveMsg(`Error: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setSaving(false);
    }
  };

  if (loading)
    return <p className="text-innate-muted text-sm">Loading...</p>;
  if (error || !data)
    return <p className="text-innate-orange text-sm">Error: {error}</p>;

  const meta = data.metadata;

  return (
    <div>
      <button
        onClick={onBack}
        className="flex items-center gap-1 text-sm text-innate-muted hover:text-black mb-4 transition-colors"
      >
        <ArrowLeft size={16} /> Back to skills
      </button>

      <div className="flex items-center justify-between mb-5">
        <h2 className="text-lg font-bold">{meta.name as string}</h2>
        <div className="flex items-center gap-2">
          {saveMsg && (
            <span
              className={`text-xs font-medium ${saveMsg.startsWith("Error") ? "text-innate-orange" : "text-innate-purple"}`}
            >
              {saveMsg}
            </span>
          )}
          <button
            onClick={handleSave}
            disabled={saving || !!jsonError}
            className="flex items-center gap-1.5 px-4 py-1.5 rounded-full text-sm font-semibold bg-innate-purple text-white hover:bg-innate-purple-hover transition-colors disabled:opacity-40"
          >
            <Save size={14} /> Save
          </button>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Editable fields */}
        <section className="space-y-4">
          <h3 className="text-xs font-semibold text-innate-muted uppercase tracking-wider border-b border-innate-border pb-1">
            Editable Fields
          </h3>
          <Field
            label="Name"
            value={edits.name as string}
            onChange={(v) => setEdits({ ...edits, name: v })}
          />
          <Field
            label="Guidelines"
            value={edits.guidelines as string}
            onChange={(v) => setEdits({ ...edits, guidelines: v })}
            multiline
          />
          <Field
            label="Guidelines When Running"
            value={edits.guidelines_when_running as string}
            onChange={(v) =>
              setEdits({ ...edits, guidelines_when_running: v })
            }
            multiline
          />

          <div>
            <label className="block text-[0.7rem] font-semibold text-innate-muted uppercase tracking-wide mb-1">
              Execution (JSON)
            </label>
            <textarea
              value={executionJson}
              onChange={(e) => {
                setExecutionJson(e.target.value);
                try {
                  JSON.parse(e.target.value);
                  setJsonError(null);
                } catch {
                  setJsonError("Invalid JSON");
                }
              }}
              rows={8}
              spellCheck={false}
              className={`w-full border rounded-md px-3 py-2 text-sm font-mono focus:outline-none transition-colors resize-y ${
                jsonError
                  ? "border-innate-orange focus:border-innate-orange"
                  : "border-innate-border focus:border-innate-purple"
              }`}
            />
            {jsonError && (
              <p className="text-xs text-innate-orange mt-1">{jsonError}</p>
            )}
          </div>
        </section>

        {/* Read-only fields */}
        <section className="space-y-4">
          <h3 className="text-xs font-semibold text-innate-muted uppercase tracking-wider border-b border-innate-border pb-1">
            Read-Only
          </h3>
          <ReadOnly label="Directory" value={data.dir_name} />
          <ReadOnly label="Type" value={meta.type as string} />
          <ReadOnly
            label="Training Skill ID"
            value={(meta.training_skill_id as string) || "—"}
            mono
          />

          {data.dataset_metadata && (
            <div>
              <p className="text-[0.7rem] font-semibold text-innate-muted uppercase tracking-wide mb-1">
                Dataset
              </p>
              <div className="bg-innate-panel border border-innate-border rounded-md p-3 space-y-1 text-xs">
                <div className="flex">
                  <span className="text-innate-muted w-28 shrink-0">Episodes</span>
                  <span>
                    {(data.dataset_metadata as Record<string, unknown>)
                      .number_of_episodes as number}
                  </span>
                </div>
                <div className="flex">
                  <span className="text-innate-muted w-28 shrink-0">Type</span>
                  <span>
                    {(data.dataset_metadata as Record<string, unknown>)
                      .dataset_type as string}
                  </span>
                </div>
                <div className="flex">
                  <span className="text-innate-muted w-28 shrink-0">Frequency</span>
                  <span>
                    {(data.dataset_metadata as Record<string, unknown>)
                      .data_frequency as number}{" "}
                    Hz
                  </span>
                </div>
              </div>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}

function Field({
  label,
  value,
  onChange,
  multiline,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  multiline?: boolean;
}) {
  return (
    <div>
      <label className="block text-[0.7rem] font-semibold text-innate-muted uppercase tracking-wide mb-1">
        {label}
      </label>
      {multiline ? (
        <textarea
          value={value || ""}
          onChange={(e) => onChange(e.target.value)}
          rows={3}
          className="w-full border border-innate-border rounded-md px-3 py-2 text-sm focus:border-innate-purple focus:outline-none transition-colors resize-y"
        />
      ) : (
        <input
          type="text"
          value={value || ""}
          onChange={(e) => onChange(e.target.value)}
          className="w-full border border-innate-border rounded-md px-3 py-2 text-sm focus:border-innate-purple focus:outline-none transition-colors"
        />
      )}
    </div>
  );
}

function ReadOnly({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div>
      <p className="text-[0.7rem] font-semibold text-innate-muted uppercase tracking-wide mb-0.5">
        {label}
      </p>
      <p className={`text-sm ${mono ? "font-mono text-gray-600" : "text-gray-800"}`}>
        {value || "—"}
      </p>
    </div>
  );
}
