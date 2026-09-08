import assert from "node:assert/strict";
import { createAgentSheet } from "../js/agent/agentSheet.js";

class Element extends EventTarget {
  style = {height:""}; attributes = {}; children = new Map();
  classes = new Set();
  classList = {
    add: name => this.classes.add(name), remove: (...names) => names.forEach(name => this.classes.delete(name)),
    toggle: (name, on) => on ? this.classes.add(name) : this.classes.delete(name),
  };
  setAttribute(key, value) {this.attributes[key] = value;}
  querySelector(selector) {return this.children.get(selector) ?? null;}
  setPointerCapture() {}
  remove() {}
}
const header = new Element(), grab = new Element(), name = new Element(), actions = new Element();
header.children = new Map([[".agent-sheet-grab",grab],[".agent-sheet-name",name],[".agent-sheet-action",actions]]);
header.getBoundingClientRect = () => ({height:24});
const panel = new Element();
panel.parentElement = {clientHeight:800}; panel.prepend = () => {};
panel.children.set(".first-mission-challenge", {getBoundingClientRect:()=>({height:50})});
panel.getBoundingClientRect = () => ({height:parseFloat(panel.style.height) || (panel.classes.has("sheet-half") ? 400 : 76)});
globalThis.document = {createElement:()=>header};
globalThis.window = {innerHeight:800};
const sheet = createAgentSheet(panel);
sheet.setEnabled(true);
const pointer = (type, y) => {
  const event = new Event(type);Object.assign(event,{isPrimary:true,button:0,pointerId:1,clientY:y});grab.dispatchEvent(event);
};
grab.dispatchEvent(new Event("click"));
assert.equal(panel.classes.has("sheet-half"),true);
// At 245px, half height is nearer than the new 76px closed strip. The old
// hard-coded 52px header would incorrectly snap this same gesture closed.
pointer("pointerdown",0);pointer("pointermove",155);pointer("pointerup",155);
grab.dispatchEvent(new Event("click")); // The drag's trailing click is swallowed.
assert.equal(panel.classes.has("sheet-half"),true);
pointer("pointerdown",0);pointer("pointermove",700);
assert.equal(panel.style.height,"76px");
pointer("pointerup",700);grab.dispatchEvent(new Event("click"));
assert.equal(panel.classes.has("sheet-closed"),true);
assert.equal(grab.attributes["aria-expanded"],"false");
grab.dispatchEvent(new Event("click"));assert.equal(panel.classes.has("sheet-half"),true);
sheet.setEnabled(false);assert.equal(header.hidden,true);
sheet.destroy();
console.log("ok - measured mission strip controls drag snapping, trailing click and tap reopening");
