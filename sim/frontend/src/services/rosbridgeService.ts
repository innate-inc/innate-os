export interface RobotAgent {
  id: string;
  display_name: string;
  display_icon: string | null;
  prompt: string;
  skills: string[];
}

export interface AvailableAgentsResponse {
  agents: RobotAgent[];
  current_agent_id: string | null;
  startup_agent_id: string | null;
  error?: string;
}

interface GetAvailableDirectivesValues {
  directives?: unknown;
  current_directive?: string | null;
  startup_directive?: string | null;
}

interface RosbridgeServiceResponse<T> {
  op?: string;
  id?: string;
  result?: boolean;
  values?: T;
}

const DEFAULT_SERVICE_TIMEOUT_MS = 10000;
const DEFAULT_PUBLISH_LINGER_MS = 120;

export function callRosbridgeService<T = Record<string, unknown>>(
  wsUrl: string,
  service: string,
  args: Record<string, unknown> = {},
  timeoutMs: number = DEFAULT_SERVICE_TIMEOUT_MS,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const callId = `svc_${service.replace(/[^a-zA-Z0-9]/g, "_")}_${Date.now()}_${Math.floor(
      Math.random() * 1e5,
    )}`;

    const ws = new WebSocket(wsUrl);
    let settled = false;
    let timeoutHandle: number | null = null;

    const finish = (fn: () => void) => {
      if (settled) {
        return;
      }
      settled = true;
      if (timeoutHandle !== null) {
        window.clearTimeout(timeoutHandle);
        timeoutHandle = null;
      }
      try {
        ws.close();
      } catch {
        // ignore
      }
      fn();
    };

    timeoutHandle = window.setTimeout(() => {
      finish(() => {
        reject(new Error(`Service ${service} timed out after ${timeoutMs}ms`));
      });
    }, timeoutMs);

    ws.onopen = () => {
      ws.send(
        JSON.stringify({
          op: "call_service",
          id: callId,
          service,
          args,
        }),
      );
    };

    ws.onmessage = (event) => {
      try {
        const message = JSON.parse(
          event.data as string,
        ) as RosbridgeServiceResponse<T>;
        if (message.op !== "service_response" || message.id !== callId) {
          return;
        }

        if (message.result === false) {
          finish(() => {
            reject(new Error(`Service ${service} returned result=false`));
          });
          return;
        }

        finish(() => {
          resolve((message.values ?? {}) as T);
        });
      } catch {
        // Ignore non-JSON/unrelated messages.
      }
    };

    ws.onerror = () => {
      finish(() => {
        reject(new Error(`Failed to connect to ROSBridge at ${wsUrl}`));
      });
    };

    ws.onclose = () => {
      if (!settled) {
        finish(() => {
          reject(new Error(`Connection closed before ${service} completed`));
        });
      }
    };
  });
}

export function publishRosbridgeTopic(
  wsUrl: string,
  topic: string,
  msg: Record<string, unknown>,
  lingerMs: number = DEFAULT_PUBLISH_LINGER_MS,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    let settled = false;
    let closeHandle: number | null = null;

    const finish = (fn: () => void) => {
      if (settled) {
        return;
      }
      settled = true;
      if (closeHandle !== null) {
        window.clearTimeout(closeHandle);
        closeHandle = null;
      }
      try {
        ws.close();
      } catch {
        // ignore
      }
      fn();
    };

    ws.onopen = () => {
      ws.send(
        JSON.stringify({
          op: "publish",
          topic,
          msg,
        }),
      );

      closeHandle = window.setTimeout(() => {
        finish(() => resolve());
      }, lingerMs);
    };

    ws.onerror = () => {
      finish(() => {
        reject(new Error(`Failed to publish to ${topic} via ${wsUrl}`));
      });
    };
  });
}

export async function getAvailableAgentsDirect(
  wsUrl: string,
): Promise<AvailableAgentsResponse> {
  const values = await callRosbridgeService<GetAvailableDirectivesValues>(
    wsUrl,
    "/brain/get_available_directives",
    {},
  );

  let directivesJson = "[]";
  if (Array.isArray(values.directives) && typeof values.directives[0] === "string") {
    directivesJson = values.directives[0];
  } else if (typeof values.directives === "string") {
    directivesJson = values.directives;
  }

  let parsedDirectives: unknown = [];
  try {
    parsedDirectives = JSON.parse(directivesJson);
  } catch {
    parsedDirectives = [];
  }

  const agents: RobotAgent[] = Array.isArray(parsedDirectives)
    ? parsedDirectives
        .filter(
          (entry): entry is Record<string, unknown> =>
            !!entry && typeof entry === "object",
        )
        .map((entry) => ({
          id: String(entry.id ?? ""),
          display_name: String(entry.display_name ?? entry.id ?? ""),
          display_icon:
            typeof entry.display_icon === "string" || entry.display_icon === null
              ? entry.display_icon
              : null,
          prompt: String(entry.prompt ?? ""),
          skills: Array.isArray(entry.skills)
            ? entry.skills
                .filter((skill): skill is string => typeof skill === "string")
                .map((skill) => skill)
            : [],
        }))
    : [];

  return {
    agents,
    current_agent_id: values.current_directive ?? null,
    startup_agent_id: values.startup_directive ?? null,
  };
}

export async function setDirectiveDirect(
  wsUrl: string,
  directive: string,
): Promise<void> {
  await publishRosbridgeTopic(wsUrl, "/brain/set_directive", {
    data: directive,
  });
}

export async function setBrainActiveDirect(
  wsUrl: string,
  active: boolean,
): Promise<void> {
  await callRosbridgeService(wsUrl, "/brain/set_brain_active", { data: active });
}

export async function resetBrainDirect(
  wsUrl: string,
  memoryState?: string,
): Promise<void> {
  await callRosbridgeService(wsUrl, "/brain/reset_brain", {
    memory_state: memoryState ?? "",
  });
}

export async function stopAgentDirect(wsUrl: string): Promise<void> {
  await setBrainActiveDirect(wsUrl, false);
}
