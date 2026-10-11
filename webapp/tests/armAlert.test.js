// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Run with node --test webapp/tests/armAlert.test.js.
import assert from "node:assert/strict";
import { test } from "node:test";
import { createArmAlert } from "../js/armAlert.js";

class Element {
  children = [];
  listeners = {};
  classList = { toggle() {} };
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.append(child); }
  setAttribute() {}
  addEventListener(name, fn) { this.listeners[name] = fn; }
  remove() { this.removed = true; }
  click() { return this.listeners.click(); }
}
const fault = "Servo 3 hardware error: overload — reboot advice";
function setup() {
  globalThis.document = { createElement: () => new Element(), body: new Element() };
  const confirms = [];
  globalThis.window = { confirm: (message) => { confirms.push(message); return true; } };
  const calls = [];
  let receive, stateChange, unsubscribed = 0;
  const client = {
    state: "connected",
    subscribe(topic, fn) { receive = fn; return () => unsubscribed++; },
    onStateChange(fn) { stateChange = fn; return () => unsubscribed++; },
    async callService(...args) { calls.push(args); return { success: true }; },
  };
  const alert = createArmAlert(client);
  const [head, text, servos, arm] = alert.el.children;
  return { client, alert, text, servos, arm, calls, confirms,
    status: (error = fault, is_ok = false) => receive({ error, is_ok }),
    disconnect() { client.state = "disconnected"; stateChange(); },
    dismiss: () => head.children[2].click(),
    unsubscribed: () => unsubscribed,
  };
}

test("only hardware faults show both recovery choices; dismissal resets on clear", () => {
  const f = setup();
  for (const error of ["All servos nominal", "Servo 3 high load (95%)", "Servo 3 high temperature (70 C)"]) {
    f.status(error);
    assert.equal(f.alert.el.hidden, true);
  }
  f.status();
  assert.equal(f.text.textContent, "Servo 3 hardware error: overload");
  assert.equal(f.servos.textContent, "Reboot servo(s)");
  assert.equal(f.arm.textContent, "Reboot arm");
  f.dismiss();
  f.status();
  assert.equal(f.alert.el.hidden, true);
  f.status("All servos nominal", true);
  f.status();
  assert.equal(f.alert.el.hidden, false);
  f.disconnect();
  assert.equal(f.alert.el.hidden, true);
  f.alert.destroy();
  assert.equal(f.unsubscribed(), 2);
  assert.equal(f.alert.el.removed, true);
});

test("targeted recovery delegates live fault selection to fix_error only", async () => {
  for (const status of ["fixed", "no_errors"]) {
    const f = setup();
    f.client.callService = async (...args) => {
      f.calls.push(args);
      return { success: true, message: JSON.stringify({ status, error_ids: [3, 5] }) };
    };
    f.status();
    await f.servos.click();
    assert.deepEqual(f.calls, [["/mars/arm/fix_error", {}, 20000]]);
    assert.match(f.confirms[0], /only the servo/);
    assert.equal(f.alert.el.hidden, true);
  }
});

test("canceling either confirmation sends no request", async () => {
  const f = setup();
  window.confirm = () => false;
  f.status();
  await f.servos.click();
  await f.arm.click();
  assert.deepEqual(f.calls, []);
  assert.equal(f.alert.el.hidden, false);
});

test("pending recovery disables both actions and does not erase a newer fault", async () => {
  const f = setup();
  let finish;
  f.client.callService = (...args) => {
    f.calls.push(args);
    return new Promise((resolve) => { finish = resolve; });
  };
  f.status();
  const pending = f.servos.click();
  assert.equal(f.servos.disabled, true);
  assert.equal(f.arm.disabled, true);
  await f.servos.click();
  await f.arm.click();
  assert.equal(f.calls.length, 1);
  f.status("Servo 5 hardware error: overheating");
  finish({ success: true });
  await pending;
  assert.equal(f.alert.el.hidden, false);
  assert.match(f.text.textContent, /Servo 5/);
  assert.equal(f.servos.disabled, false);
  assert.equal(f.arm.disabled, false);
});

test("service failure, malformed reply and timeout keep retry available without global recovery", async () => {
  for (const result of [{ success: false, message: "Servo offline" }, undefined, new Error("Timed out")]) {
    const f = setup();
    f.client.callService = async (...args) => {
      f.calls.push(args);
      if (result instanceof Error) throw result;
      return result;
    };
    f.status();
    await f.servos.click();
    assert.equal(f.alert.el.hidden, false);
    assert.match(f.text.textContent, /Servo offline|Servo reboot failed|Timed out/);
    assert.equal(f.servos.disabled, false);
    assert.equal(f.arm.disabled, false);
    assert.equal(f.calls.length, 1);
    f.client.callService = async () => ({ success: true });
    await f.servos.click();
    assert.equal(f.alert.el.hidden, true);
  }
});

test("full-arm reboot retains reboot then torque-on sequence", async () => {
  const f = setup();
  f.status();
  await f.arm.click();
  assert.deepEqual(f.calls, [["/mars/arm/reboot", {}, 20000], ["/mars/arm/torque_on", {}]]);
  assert.match(f.confirms[0], /head recenters/);
  assert.equal(f.alert.el.hidden, true);
});
