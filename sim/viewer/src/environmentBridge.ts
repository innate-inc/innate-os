import type { EnvironmentRoster } from "./physics/worldStateController";
import type { SimSession } from "./simSession";

// Versioned, deliberately small contract with sim-broker/public/environment.js.
// The embedding document may select a public world, never issue arbitrary
// simulator commands or receive raw errors, asset paths, or robot state.
const CHANNEL = "innate:environment:v1";
type ViewState = "loading" | "ready" | "failed";

export function createEnvironmentBridge(session: SimSession, retryView: () => void) {
  let parentOrigin: string | null = null;
  if (window.parent !== window && document.referrer) {
    const url = new URL(document.referrer);
    if (["https:", "http:"].includes(url.protocol)) parentOrigin = url.origin;
  }
  let roster: EnvironmentRoster | null = null;
  let view: { id: string; state: ViewState } | null = null;
  let requested: { id: string; at: number } | null = null;
  let requestFailed = false;

  const publish = () => {
    if (!parentOrigin) return;
    // A lost command must not leave the control pending forever. A later
    // server roster still wins, including a switch completed after reconnect.
    if (requested && Date.now() - requested.at > 8000) {
      requested = null;
      requestFailed = true;
    }
    const connected = session.environmentConnected;
    const pending = requested?.id ?? (roster?.switch?.state === "loading" ? roster.switch.id : null);
    window.parent.postMessage({
      channel: CHANNEL,
      type: "state",
      current: roster?.environment?.id ?? null,
      environments: roster?.environments.map(({ id, display_name }) => ({ id, display_name })) ?? [],
      pending,
      state: !connected ? "disconnected" : pending ? "loading"
        : requestFailed || roster?.switch?.state === "failed" ? "failed"
        : view?.id === roster?.environment?.id ? view?.state : "loading",
      retryView: connected && view?.state === "failed",
    }, parentOrigin);
  };
  const unsubscribe = session.onEnvironment((next) => {
    roster = next;
    requested = null;
    requestFailed = false;
    publish();
  });
  const onMessage = (event: MessageEvent) => {
    if (!parentOrigin || event.source !== window.parent || event.origin !== parentOrigin) return;
    const data = event.data;
    if (!data || data.channel !== CHANNEL) return;
    if (data.type === "get-state") return publish();
    if (data.type === "retry-view" && view?.state === "failed") return retryView();
    if (data.type !== "switch" || typeof data.id !== "string") return;
    if (!session.environmentConnected || requested || roster?.switch?.state === "loading" || view?.state === "loading") return;
    if (data.id === roster?.environment?.id || !roster?.environments.some(({ id }) => id === data.id)) return;
    requested = { id: data.id, at: Date.now() };
    requestFailed = false;
    session.switchEnvironment(data.id);
    publish();
  };
  window.addEventListener("message", onMessage);
  return {
    updateView(id: string, state: ViewState) {
      view = { id, state };
      publish();
    },
    destroy() {
      unsubscribe();
      window.removeEventListener("message", onMessage);
    },
  };
}
