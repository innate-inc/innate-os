import { useEffect, useState } from "react";
import { ArrowLeft } from "lucide-react";
import { api } from "../../api";

interface RunData {
  skill_dir_name: string;
  skill_id: string;
  run_id: number;
  status: string;
  daemon_state: string | null;
  error_message: string | null;
  created_at: string | null;
  updated_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  instance_ip: string | null;
  instance_type: string | null;
  training_params: Record<string, unknown> | null;
}

const ACTIVE = ["waiting_for_approval", "approved", "booting", "running"];

interface Props {
  skillName: string;
  runId: number;
  onBack: () => void;
}

export default function RunDetail({ skillName, runId, onBack }: Props) {
  const [run, setRun] = useState<RunData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchRun = () => {
    api
      .get<RunData>(`/api/training/runs/${skillName}/${runId}`)
      .then(setRun)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    setLoading(true);
    fetchRun();
  }, [skillName, runId]);

  useEffect(() => {
    if (!run || !ACTIVE.includes(run.status)) return;
    const interval = setInterval(fetchRun, 15000);
    return () => clearInterval(interval);
  }, [run?.status]);

  if (loading)
    return <p className="text-innate-muted text-sm">Loading...</p>;
  if (error || !run)
    return <p className="text-innate-orange text-sm">Error: {error}</p>;

  const isActive = ACTIVE.includes(run.status);

  return (
    <div>
      <button
        onClick={onBack}
        className="flex items-center gap-1 text-sm text-innate-muted hover:text-black mb-4 transition-colors"
      >
        <ArrowLeft size={16} /> Back to runs
      </button>

      <div className="flex items-center gap-3 mb-5">
        <h2 className="text-lg font-bold">
          Run #{run.run_id}
        </h2>
        <StatusBadge status={run.status} />
        {isActive && (
          <span className="inline-block w-2 h-2 rounded-full bg-green-500 animate-pulse" />
        )}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <section>
          <h3 className="text-xs font-semibold text-innate-muted uppercase tracking-wider border-b border-innate-border pb-1 mb-3">
            Status
          </h3>
          <dl className="space-y-2 text-sm">
            <Detail label="Status" value={run.status.replace(/_/g, " ")} />
            {run.daemon_state && (
              <Detail label="Daemon State" value={run.daemon_state} />
            )}
            {run.error_message && (
              <Detail label="Error" value={run.error_message} error />
            )}
            <Detail label="Created" value={fmtTime(run.created_at)} />
            <Detail label="Updated" value={fmtTime(run.updated_at)} />
            {run.started_at && (
              <Detail label="Started" value={fmtTime(run.started_at)} />
            )}
            {run.finished_at && (
              <Detail label="Finished" value={fmtTime(run.finished_at)} />
            )}
            {run.started_at && (
              <Detail
                label="Duration"
                value={duration(run.started_at, run.finished_at)}
              />
            )}
          </dl>
        </section>

        <section>
          <h3 className="text-xs font-semibold text-innate-muted uppercase tracking-wider border-b border-innate-border pb-1 mb-3">
            Infrastructure
          </h3>
          <dl className="space-y-2 text-sm">
            <Detail label="Skill ID" value={run.skill_id} mono />
            <Detail label="Skill Dir" value={run.skill_dir_name} mono />
            {run.instance_ip && (
              <Detail label="Instance IP" value={run.instance_ip} mono />
            )}
            {run.instance_type && (
              <Detail label="Instance Type" value={run.instance_type} />
            )}
          </dl>

          {run.training_params &&
            Object.keys(run.training_params).length > 0 && (
              <div className="mt-4">
                <h3 className="text-xs font-semibold text-innate-muted uppercase tracking-wider border-b border-innate-border pb-1 mb-3">
                  Training Params
                </h3>
                <div className="bg-innate-panel border border-innate-border rounded-md p-3 space-y-1">
                  {Object.entries(run.training_params).map(([k, v]) => (
                    <div key={k} className="flex text-xs">
                      <span className="text-innate-muted w-32 shrink-0">
                        {k}
                      </span>
                      <span className="font-mono text-gray-700 break-all">
                        {typeof v === "object"
                          ? JSON.stringify(v)
                          : String(v)}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}
        </section>
      </div>
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    waiting_for_approval: "bg-innate-orange text-white",
    approved: "bg-innate-purple text-white",
    booting: "bg-innate-purple-hover text-white",
    running: "bg-innate-purple-hover text-white",
    done: "bg-innate-border text-black",
    rejected: "bg-black text-white",
    downloaded: "bg-innate-border text-gray-600",
  };
  const c = colors[status] || "bg-innate-border text-gray-600";
  return (
    <span
      className={`inline-block px-2 py-0.5 rounded-full text-[0.68rem] font-semibold uppercase ${c}`}
    >
      {status.replace(/_/g, " ")}
    </span>
  );
}

function Detail({
  label,
  value,
  mono,
  error,
}: {
  label: string;
  value: string;
  mono?: boolean;
  error?: boolean;
}) {
  return (
    <div className="flex">
      <dt className="text-innate-muted w-28 shrink-0 text-xs font-medium uppercase tracking-wide">
        {label}
      </dt>
      <dd
        className={`${mono ? "font-mono text-xs" : ""} ${error ? "text-innate-orange" : "text-gray-800"} break-all`}
      >
        {value || "—"}
      </dd>
    </div>
  );
}

function fmtTime(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso.endsWith("Z") ? iso : iso + "Z");
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function duration(start: string | null, end: string | null): string {
  if (!start) return "—";
  const s = new Date(start.endsWith("Z") ? start : start + "Z");
  const e = end
    ? new Date(end.endsWith("Z") ? end : end + "Z")
    : new Date();
  const secs = Math.floor((e.getTime() - s.getTime()) / 1000);
  if (secs < 60) return `${secs}s`;
  const m = Math.floor(secs / 60);
  if (m < 60) return `${m}m ${secs % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}
