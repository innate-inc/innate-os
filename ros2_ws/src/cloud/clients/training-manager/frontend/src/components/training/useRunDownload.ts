import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../api";

export interface DownloadStatus {
  stage: string;
  message: string;
  done: boolean;
  error: string | null;
  progress: number;
}

const IDLE: DownloadStatus = {
  stage: "idle",
  message: "",
  done: true,
  error: null,
  progress: 0,
};

const POLL_INTERVAL_MS = 1500;

export function useRunDownload(
  skillName: string,
  runId: number,
  onComplete?: () => void,
) {
  const [status, setStatus] = useState<DownloadStatus>(IDLE);
  const [active, setActive] = useState(false);
  const submittingRef = useRef(false);
  const onCompleteRef = useRef(onComplete);

  useEffect(() => {
    onCompleteRef.current = onComplete;
  }, [onComplete]);

  useEffect(() => {
    if (!active) return;

    let cancelled = false;
    const poll = async () => {
      try {
        const s = await api.get<DownloadStatus>(
          `/api/training/runs/${skillName}/${runId}/download-status`,
        );
        if (cancelled) return;
        setStatus(s);
        if (s.done) {
          setActive(false);
          onCompleteRef.current?.();
        }
      } catch (e) {
        if (cancelled) return;
        setStatus({
          stage: "error",
          message: (e as Error).message,
          done: true,
          error: (e as Error).message,
          progress: 0,
        });
        setActive(false);
      }
    };

    poll();
    const interval = setInterval(poll, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [active, skillName, runId]);

  const start = useCallback(async () => {
    // Synchronous guard against double-clicks: the `active` state is only
    // set after the POST resolves, so relying on it alone leaves a window
    // where a second click fires another POST (which gets a 409).
    if (submittingRef.current) return;
    submittingRef.current = true;
    setStatus({
      stage: "starting",
      message: "Starting...",
      done: false,
      error: null,
      progress: 0,
    });
    try {
      await api.post<{ status: string }>(
        `/api/training/runs/${skillName}/${runId}/download`,
        {},
      );
      setActive(true);
    } catch (e) {
      setStatus({
        stage: "error",
        message: (e as Error).message,
        done: true,
        error: (e as Error).message,
        progress: 0,
      });
    } finally {
      submittingRef.current = false;
    }
  }, [skillName, runId]);

  return { status, active, start };
}
