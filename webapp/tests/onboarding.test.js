// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
import assert from "node:assert/strict";
import { createAgentOnboarding, FIRST_MISSIONS } from "../js/agent/agentOnboarding.js";
import { FIRST_RUN_KEY, readFirstRun, shouldAutoStartOnboarding } from "../js/onboarding.js";

class Element extends EventTarget {
  children = []; dataset = {}; hidden = false; parent = null; textContent = "";
  classList = {values:new Set(), toggle:(name,on)=> on ? this.classList.values.add(name) : this.classList.values.delete(name), contains:name=>this.classList.values.has(name)};
  append(...children) {this.children.push(...children); for (const child of children) child.parent=this;}
  replaceChildren(...children) {this.children=[]; this.append(...children);}
  setAttribute() {}
  remove() {if(this.parent) this.parent.children=this.parent.children.filter(c=>c!==this);}
  click() {this.dispatchEvent(new Event("click"));}
  find(predicate) {if(predicate(this)) return this; for(const child of this.children){const match=child.find(predicate);if(match)return match;}}
}
const storage = new Map();
globalThis.localStorage = {getItem:k=>storage.get(k)??null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
globalThis.document = Object.assign(new EventTarget(),{body:new Element(),createElement:()=>new Element()});
globalThis.window = new EventTarget();
const flush = async()=>{for(let i=0;i<8;i++)await new Promise(resolve=>setImmediate(resolve));};
function simulator() {
  const callbacks = {environment:new Set(),challenge:new Set(),agent:new Set()};
  let env = {environment:{id:"apartment"},switch:null};
  let challenge = {list:FIRST_MISSIONS,active:null};
  let state = {agents:[{id:"intro_agent"}],currentDirective:"",brainActive:false};
  const calls = {starts:[],switches:[],directives:[],aborts:[],begins:[]};
  const emitChallenge = value=>{challenge=value; for(const cb of callbacks.challenge)cb(value);};
  const emitEnvironment = value=>{env=value;for(const cb of callbacks.environment)cb(value);};
  const session = {
    onEnvironment(cb){callbacks.environment.add(cb);cb(env);return()=>callbacks.environment.delete(cb);},
    onChallenge(cb){callbacks.challenge.add(cb);cb(challenge);return()=>callbacks.challenge.delete(cb);},
    switchEnvironment(id){calls.switches.push(id);emitEnvironment({environment:{id},switch:null});},
    startChallenge(id,attempt_id){calls.starts.push({id,attempt_id});emitChallenge({list:FIRST_MISSIONS,active:{id,attempt_id,state:"running"}});},
    abortChallenge(id){calls.aborts.push(id);},
  };
  const agent = {
    get:()=>state,
    subscribe(cb){callbacks.agent.add(cb);cb(state);return()=>callbacks.agent.delete(cb);},
    async setDirective(id){calls.directives.push(id);state={...state,currentDirective:id,brainActive:!!id};for(const cb of callbacks.agent)cb(state);},
  };
  function mount(enabled=true) {
    const root=new Element();
    const ros={subscribe(_topic,cb){cb({data:'{"connected":true}'});return()=>{};}};
    const flow=createAgentOnboarding(root,ros,agent,{enabled,session,onStart:(...args)=>calls.begins.push(args)});
    return {root,flow,choose:id=>root.find(el=>el.dataset.mission===id).click(),skip:()=>root.find(el=>/Skip mission|Explore on my own/.test(el.textContent)).click()};
  }
  return {mount,session,agent,calls,emitChallenge,emitEnvironment,get challenge(){return challenge;}};
}

// Three choices work through the real controller/session handshake. Arbitrary
// speech cannot gate completion, and only the local attempt can finish it.
for(const mission of FIRST_MISSIONS) {
  storage.clear();const sim=simulator();const ui=sim.mount();
  ui.choose(mission.id);await flush();
  assert.equal(sim.calls.starts.length,1);
  assert.equal(sim.calls.starts[0].id,mission.id);
  assert.equal(sim.agent.get().currentDirective,"intro_agent");
  ui.flow.onUserMessage("Can you try that again?");
  sim.emitChallenge({...sim.challenge,active:{...sim.challenge.active,attempt_id:"foreign",state:"passed"}});
  assert.equal(ui.flow.isActive(),true);
  sim.emitChallenge({...sim.challenge,active:{id:mission.id,attempt_id:sim.calls.starts[0].attempt_id,state:"passed"}});
  assert.equal(ui.flow.isActive(),false);
  assert.equal(shouldAutoStartOnboarding(),false);
  ui.flow.destroy();
}
// Reopen the page in flight: no restart, prop placement, or second agent start.
storage.clear();const sim=simulator();let ui=sim.mount();ui.choose("put_it_away");await flush();
const attempt=sim.calls.starts[0].attempt_id;
ui.flow.destroy();ui=sim.mount();await flush();
assert.equal(sim.calls.starts.length,1);assert.equal(sim.calls.directives.length,1);
assert.deepEqual(sim.calls.begins.map(([fresh])=>fresh),[true,false]);
assert.equal(readFirstRun().attemptId,attempt);
ui.skip();await flush();assert.deepEqual(sim.calls.aborts,[attempt]);assert.equal(sim.agent.get().brainActive,false);
ui.flow.destroy();ui=sim.mount();assert.equal(ui.flow.isActive(),false);ui.flow.destroy();
// Skip before any selection must never abort another browser's active mission.
storage.clear();const other=simulator();other.emitChallenge({list:FIRST_MISSIONS,active:{id:"put_it_away",state:"passed"}});
ui=other.mount();assert.equal(ui.flow.isActive(),true);ui.skip();await flush();assert.equal(other.calls.aborts.length,0);ui.flow.destroy();
// Skip while the environment is loading: no delayed start can rebuild the scene.
storage.clear();const slow=simulator();slow.session.switchEnvironment=id=>slow.calls.switches.push(id);
ui=slow.mount();ui.choose("way_out");await flush();ui.skip();await flush();
slow.emitEnvironment({environment:{id:"backrooms"},switch:null});await flush();assert.equal(slow.calls.starts.length,0);ui.flow.destroy();
// An activation already in flight survives closing the page, but never Skip.
for (const skip of [false,true]) {
  storage.clear();const pending=simulator();
  let release;
  const setDirective=pending.agent.setDirective;
  pending.agent.setDirective=async id=>{
    if(id) await new Promise(resolve=>{release=resolve;});
    return setDirective(id);
  };
  ui=pending.mount();ui.choose("put_it_away");await flush();
  if(skip) ui.skip(); else ui.flow.destroy();
  release();await flush();
  assert.equal(pending.agent.get().brainActive,!skip);
  if(skip) ui.flow.destroy();
}
// Physical robot visits do not start the first mission or activate the brain.
storage.clear();const hardware=simulator();ui=hardware.mount(false);await flush();assert.equal(ui.flow.isActive(),false);assert.equal(hardware.calls.directives.length,0);ui.flow.destroy();
storage.set(FIRST_RUN_KEY,'{"id":"put_it_away","phase":"playing","attemptId":"broken"}');assert.equal(readFirstRun(),null);
console.log("ok - first missions: all choices, exact attempt, natural chat, reload, skip, and hardware guard");

// A denied write still preserves this tab's attempt across route remounts.
storage.clear();
const setItemBeforeDenial=localStorage.setItem;
localStorage.setItem=()=>{throw Error("Storage denied");};
const denied=simulator();
ui=denied.mount();ui.choose("put_it_away");await flush();
ui.flow.destroy();ui=denied.mount();await flush();
assert.equal(denied.calls.starts.length,1);
ui.skip();await flush();ui.flow.destroy();ui=denied.mount();
assert.equal(ui.flow.isActive(),false);ui.flow.destroy();
localStorage.setItem=setItemBeforeDenial;

// Terminal completion travels through the pinned parent, across container origins.
for (const mode of ["fresh", "completed", "playing", "locked"]) {
  storage.clear();
  const module = await import(`../js/onboarding.js?broker=${mode}`);
  if (mode === "playing") storage.set(FIRST_RUN_KEY, JSON.stringify({id:"way_out", phase:"playing", attemptId:"00000000-0000-0000-0000-000000000001", startedAt:1}));
  const sent=[];
  document.referrer="https://sim.example/session";
  window.parent={postMessage:(data,origin)=>sent.push({data,origin})};
  const originalSet=localStorage.setItem;
  if(mode === "locked") localStorage.setItem=()=>{throw Error("Storage blocked");};
  const ready=module.initializeFirstRunCompletion();
  const request=sent[0].data;
  const reply=(origin,source,phase,requestId=request.requestId)=>{
    const event=new Event("message");
    Object.assign(event,{origin,source,data:{channel:request.channel,type:"completion",requestId,phase}});
    window.dispatchEvent(event);
  };
  reply("https://wrong.example",window.parent,"done");
  reply("https://sim.example",{},"done");
  reply("https://sim.example",window.parent,"done","wrong-request");
  assert.equal(module.shouldAutoStartOnboarding(),true);
  reply("https://sim.example",window.parent,mode === "fresh" ? null : "done");
  await ready;
  assert.equal(module.shouldAutoStartOnboarding(),["fresh","playing"].includes(mode));
  if(mode === "playing") assert.equal(module.readFirstRun().phase,"playing");
  if(["completed","locked"].includes(mode)) assert.equal(sent.at(-1).data.type,"completed");
  assert.ok(sent.every(message=>message.origin === "https://sim.example"));
  localStorage.setItem=originalSet;
}
document.referrer="";
console.log("ok - broker completion: pinned source/origin/request, new session, active attempt, and blocked storage");
