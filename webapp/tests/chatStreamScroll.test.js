// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Scroll-behavior tests for js/agent/chatStream.js — zero dependencies, plain node:
//   node tests/chatStreamScroll.test.js

import assert from "node:assert/strict";

class FakeClassList {
  /** @param {FakeElement} owner */
  constructor(owner) {
    this.owner = owner;
    this.values = new Set();
  }

  /** @param {string[]} names */
  add(...names) {
    for (const name of names) this.values.add(name);
    this.sync();
  }

  /** @param {string[]} names */
  remove(...names) {
    for (const name of names) this.values.delete(name);
    this.sync();
  }

  /** @param {string} name */
  contains(name) {
    return this.values.has(name);
  }

  /** @param {string} name @param {boolean} [force] */
  toggle(name, force) {
    const next = force === undefined ? !this.values.has(name) : force;
    if (next) this.values.add(name);
    else this.values.delete(name);
    this.sync();
    return next;
  }

  sync() {
    this.owner._className = [...this.values].join(" ");
  }
}

class FakeElement {
  constructor() {
    this.children = [];
    this.attributes = new Map();
    this.classList = new FakeClassList(this);
    this._className = "";
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
    this.textContent = "";
  }

  set className(value) {
    this._className = value;
    this.classList.values = new Set(value.split(/\s+/).filter(Boolean));
  }

  get className() {
    return this._className;
  }

  append(...nodes) {
    this.children.push(...nodes);
  }

  appendChild(node) {
    this.children.push(node);
    return node;
  }

  replaceChildren(...nodes) {
    this.children = [...nodes];
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  addEventListener() {}

  querySelectorAll() {
    return [];
  }
}

globalThis.HTMLElement = FakeElement;
globalThis.HTMLButtonElement = FakeElement;
globalThis.document = { createElement: () => new FakeElement() };

const { createChatStream } = await import("../js/agent/chatStream.js");

let passed = 0;
/** @param {string} name @param {() => void} fn */
function test(name, fn) {
  fn();
  passed += 1;
  console.log(`ok - ${name}`);
}

function fixture() {
  const chat = createChatStream();
  const stream = chat.wrap.children[0];
  stream.scrollHeight = 1000;
  stream.clientHeight = 300;
  return { chat, stream };
}

test("sending keeps the new user message at the bottom in compact mode", () => {
  const { chat, stream } = fixture();
  stream.scrollTop = 120;
  chat.addMessage("user", "hello MARS", 1);
  assert.equal(stream.scrollTop, 1000);
  chat.destroy();
});

test("incoming output follows when the reader is already at the bottom", () => {
  const { chat, stream } = fixture();
  stream.scrollTop = 650;
  chat.addMessage("robot", "hello", 2);
  assert.equal(stream.scrollTop, 1000);
  chat.destroy();
});

test("incoming output preserves manual scrollback", () => {
  const { chat, stream } = fixture();
  stream.scrollTop = 120;
  chat.addMessage("robot", "still working", 3);
  assert.equal(stream.scrollTop, 120);
  chat.destroy();
});

console.log(`\n${passed} passed`);
