import assert from "node:assert/strict";
import test from "node:test";
import { createEnvironmentBridge } from "../src/environmentBridge.ts";

test("embedding bridge follows real roster and view transitions, rejects foreign commands, and recovers", () => {
  const listeners = new Set<(event: any) => void>();
  const messages: any[] = [];
  const parent = { postMessage: (data: any, origin: string) => {
    assert.equal(origin, "https://sim.innate.bot");
    messages.push(data);
  } };
  const win = { parent, addEventListener: (_: string, cb: any) => listeners.add(cb),
    removeEventListener: (_: string, cb: any) => listeners.delete(cb) };
  const oldWindow = globalThis.window;
  const oldDocument = globalThis.document;
  const now = Date.now;
  Object.assign(globalThis, { window: win, document: { referrer: "https://sim.innate.bot/session" } });
  let rosterListener: any;
  const sent: string[] = [];
  const session = { environmentConnected: true, onEnvironment: (cb: any) => {
    rosterListener = cb;
    return () => { rosterListener = null; };
  }, switchEnvironment: (id: string) => sent.push(id) };
  let retries = 0;
  const bridge = createEnvironmentBridge(session as any, () => retries++);
  const environments = ["apartment", "backrooms", "intersection"].map(id => ({ id, display_name: id }));
  const roster = (id: string, pending: any = null) => rosterListener({ environment: { id }, environments, switch: pending });
  const command = (type: string, id?: string, origin = "https://sim.innate.bot", source: any = parent) => {
    for (const cb of listeners) cb({ origin, source, data: { channel: "innate:environment:v1", type, id } });
  };
  const state = () => messages.at(-1);
  try {
    roster("apartment");
    bridge.updateView("apartment", "ready");
    command("switch", "backrooms", "https://evil.example");
    command("switch", "backrooms", "https://sim.innate.bot", {});
    command("switch", "unknown");
    command("switch", "apartment");
    assert.deepEqual(sent, []);
    for (const id of ["backrooms", "intersection", "apartment"]) {
      command("switch", id);
      command("switch", "intersection");
      assert.equal(sent.at(-1), id);
      assert.equal(state().pending, id);
      roster(state().current, { id, state: "loading" });
      roster(id);
      bridge.updateView(id, "loading");
      assert.equal(state().state, "loading");
      bridge.updateView(id, "ready");
      assert.equal(state().current, id);
      assert.equal(state().state, "ready");
    }
    assert.equal(sent.length, 3);
    command("switch", "backrooms");
    roster("apartment", { id: "backrooms", state: "failed", message: "private failure details" });
    assert.equal(state().state, "failed");
    assert.equal(JSON.stringify(state()).includes("private failure"), false);
    command("switch", "backrooms");
    roster("backrooms");
    bridge.updateView("backrooms", "failed");
    assert.equal(state().retryView, true);
    command("retry-view");
    assert.equal(retries, 1);
    bridge.updateView("backrooms", "ready");
    session.environmentConnected = false;
    command("get-state");
    assert.equal(state().state, "disconnected");
    const count = sent.length;
    command("switch", "apartment");
    assert.equal(sent.length, count);
    session.environmentConnected = true;
    roster("intersection"); // another client changed the world while disconnected
    bridge.updateView("intersection", "ready");
    assert.equal(state().current, "intersection");
    command("switch", "apartment");
    const started = now();
    Date.now = () => started + 9000;
    command("get-state");
    assert.equal(state().state, "failed");
    assert.equal(state().pending, null);
    command("switch", "apartment");
    assert.equal(state().pending, "apartment");
  } finally {
    Date.now = now;
    bridge.destroy();
    assert.equal(listeners.size, 0);
    Object.assign(globalThis, { window: oldWindow, document: oldDocument });
  }
});
