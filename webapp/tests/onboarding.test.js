// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
import assert from "node:assert/strict";
import { createAgentOnboarding } from "../js/agent/agentOnboarding.js";
import { createChallengePanel } from "../js/agent/challengePanel.js";
import { FIRST_MISSIONS, FIRST_RUN_KEY, readFirstRun, saveFirstRun, shouldAutoStartOnboarding, startFirstRun, installMissionPicker } from "../js/onboarding.js";

class Element extends EventTarget {
  children = []; dataset = {}; hidden = false; parent = null; textContent = "";
  classList = {values:new Set(), toggle:(name,on)=> on ? this.classList.values.add(name) : this.classList.values.delete(name), contains:name=>this.classList.values.has(name)};
  get parentElement() {return this.parent;}
  append(...children) {for (const child of children) {child.remove(); this.children.push(child); child.parent=this;}}
  appendChild(child) {this.append(child); return child;}
  querySelector(selector) {return this.find(el=>el.className===selector.slice(1)) ?? null;}
  insertBefore(child, before) {child.remove(); const i=this.children.indexOf(before); if(i<0)this.children.push(child);else this.children.splice(i,0,child);child.parent=this;return child;}
  replaceChildren(...children) {this.children=[]; this.append(...children);}
  setAttribute() {}
  contains(node) { return !!this.find(el=>el===node); }
  remove() {if(this.parent) this.parent.children=this.parent.children.filter(c=>c!==this);}
  click() {this.dispatchEvent(new Event("click"));}
  find(predicate) {if(predicate(this)) return this; for(const child of this.children){const match=child.find(predicate);if(match)return match;}}
}
const storage = new Map();
globalThis.localStorage = {getItem:k=>storage.get(k)??null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
globalThis.document = Object.assign(new EventTarget(),{body:new Element(),createElement:()=>new Element()});
globalThis.window = new EventTarget();
globalThis.Node = Element;
window.matchMedia = () => ({matches:false});
const flush = async()=>{for(let i=0;i<8;i++)await new Promise(resolve=>setImmediate(resolve));};
function simulator() {
  const callbacks = {environment:new Set(),challenge:new Set(),agent:new Set()};
  let env = {environment:{id:"apartment"},switch:null};
  let challenge = {list:FIRST_MISSIONS,active:null};
  let state = {agents:[{id:"intro_agent"}],currentDirective:"",brainActive:false};
  const calls = {starts:[],switches:[],directives:[],aborts:[],begins:[],guides:[],speech:[],prompts:[]};
  const emitChallenge = value=>{
    if(value.active) value={...value,active:{goals:[{label:"Complete the scene goal",done:false}],elapsed_s:0,...value.active}};
    challenge=value; for(const cb of callbacks.challenge)cb(value);
  };
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
  function mount(enabled=true, viewGuide=false) {
    const root=new Element();
    const ros={subscribe(_topic,cb){cb({data:'{"connected":true}'});return()=>{};},advertise(){return()=>{};},publish(_topic,message){calls.speech.push(message.data);return true;}};
    const flow=createAgentOnboarding(root,ros,agent,{enabled,session,onStart:(...args)=>calls.begins.push(args),onViewAccess:viewGuide ? access=>calls.guides.push(access) : undefined,onClearSuggestions:()=>calls.prompts.push(null)});
    const panel=createChallengePanel(root,session,flow);
    const destroy=flow.destroy;
    flow.destroy=()=>{panel.destroy();destroy();};
    return {root,flow,panel,choose:id=>root.find(el=>el.dataset.mission===id).click(),skip:()=>root.find(el=>/Skip mission|Explore on my own/.test(el.textContent)).click()};
  }
  return {mount,session,agent,calls,emitChallenge,emitEnvironment,get challenge(){return challenge;}};
}

// The task starts without a camera gate. Only task motion introduces Main/Arm;
// greeting, suggestions, stale events, a foreign attempt and reload do not.
for (const mission of FIRST_MISSIONS) {
  storage.clear(); const sim=simulator(); let ui=sim.mount(true,true);
  ui.choose(mission.id); await flush();
  assert.equal(sim.calls.starts.length,1);
  assert.equal(sim.agent.get().currentDirective,"intro_agent");
  assert.equal(sim.calls.speech.length,0);
  assert.equal(sim.calls.guides.at(-1),"hidden");
  assert.deepEqual(sim.calls.prompts,[null]); // No preset before MARS speaks.
  const emit = (skill, timestamp=Date.now()/1000, status="running") => ui.flow.onSkillStatus({skill:`innate-os/${skill}`,status,timestamp});
  emit("search_memory"); emit("head_emotion"); emit("suggest_user_prompts");
  emit("pick_any_object",1); emit("pick_any_object",Date.now()/1000,"failed");
  assert.equal(sim.calls.speech.length,0);
  const own=sim.challenge;
  sim.emitChallenge({...own,active:{...own.active,attempt_id:"foreign"}});
  emit("pick_any_object"); assert.equal(sim.calls.speech.length,0);
  sim.emitChallenge(own);
  emit(mission.id === "put_it_away" ? "pick_any_object" : "navigate_to_position");
  assert.equal(sim.calls.speech.length,1);
  assert.match(sim.calls.speech[0], /Main.*Arm/);
  assert.equal(sim.calls.guides.at(-1),"cameras");
  assert.equal(readFirstRun().viewsRevealed,true);
  ui.flow.onViewChange();
  assert.equal(ui.root.classList.contains("first-mission-view-step"),false);
  emit("pick_any_object"); assert.equal(sim.calls.speech.length,1);
  const token=readFirstRun().attemptId;
  ui.flow.destroy(); ui=sim.mount(true,true); await flush();
  assert.equal(readFirstRun().attemptId,token);
  assert.equal(sim.calls.starts.length,1);
  assert.equal(sim.calls.speech.length,1);
  assert.equal(sim.calls.guides.at(-1),"cameras");
  assert.ok(sim.calls.prompts.every(prompt => prompt === null)); // Remount only clears.
  ui.skip(); await flush();
  emit("pick_any_object");
  assert.equal(sim.calls.speech.length,1);
  assert.equal(sim.calls.guides.at(-1),"all");
  ui.flow.destroy();
}
console.log("ok - task-start camera invitation: all missions, non-motion, stale/foreign events, reload and skip");

// Three choices work through the real controller/session handshake. Arbitrary
// speech cannot gate completion, and only the local attempt can finish it.
for(const mission of FIRST_MISSIONS) {
  storage.clear();const sim=simulator();const ui=sim.mount();
  ui.choose(mission.id);await flush();
  assert.equal(sim.calls.starts.length,1);
  assert.equal(sim.calls.starts[0].id,mission.id);
  assert.equal(sim.agent.get().currentDirective,"intro_agent");
  const overlay=ui.root.find(el=>el.className==="first-mission");
  const dock=ui.root.find(el=>el.className==="agent-challenge-dock");
  assert.equal(overlay.hidden,true);
  assert.equal(dock.classList.contains("open"),true);
  assert.ok(ui.root.find(el=>el.textContent==="Complete the scene goal"));
  assert.ok(ui.root.find(el=>el.textContent==="Skip mission"));
  assert.equal(ui.root.find(el=>el.textContent==="Abort"),undefined);
  assert.equal(ui.root.find(el=>el.textContent==="Retry"),undefined);
  sim.emitChallenge({...sim.challenge,active:{...sim.challenge.active,attempt_id:"foreign",state:"passed"}});
  assert.equal(ui.flow.isActive(),true);
  assert.equal(ui.root.find(el=>el.className==="challenge-banner passed"),undefined);
  sim.emitChallenge({...sim.challenge,active:{id:mission.id,attempt_id:sim.calls.starts[0].attempt_id,state:"passed"}});
  assert.equal(ui.flow.isActive(),false);
  assert.equal(shouldAutoStartOnboarding(),false);
  assert.ok(ui.root.find(el=>el.className==="challenge-banner passed"));
  ui.flow.destroy();
}
// The same guided panel moves into the phone chat sheet and back on resize;
// Skip restores the normal challenge launcher outside the sheet.
storage.clear();
const layoutSim=simulator(), layoutUi=layoutSim.mount(), sheet=new Element();
layoutUi.root.append(sheet); layoutUi.choose("put_it_away"); await flush();
const layoutDock=layoutUi.root.find(el=>el.className === "agent-challenge-dock");
layoutUi.panel.setCompactHost(sheet); assert.equal(layoutDock.parentElement,sheet);
layoutSim.emitChallenge({...layoutSim.challenge,active:{...layoutSim.challenge.active,elapsed_s:2}});
assert.equal(layoutDock.parentElement,sheet);
layoutUi.panel.setCompactHost(null); assert.equal(layoutDock.parentElement,layoutUi.root);
layoutUi.panel.setCompactHost(sheet); layoutUi.skip(); await flush();
assert.equal(layoutDock.parentElement,layoutUi.root); layoutUi.flow.destroy();
console.log("ok - challenge layout: mobile sheet, timer updates, desktop resize and Skip restore");

// Reopen the page in flight: no restart, prop placement, or second agent start.
storage.clear();const sim=simulator();let ui=sim.mount();ui.choose("put_it_away");await flush();
const attempt=sim.calls.starts[0].attempt_id;
ui.flow.destroy();ui=sim.mount();await flush();
assert.equal(sim.calls.starts.length,1);assert.equal(sim.calls.directives.length,1);
assert.deepEqual(sim.calls.begins.map(([fresh])=>fresh),[true,false]);
assert.equal(readFirstRun().attemptId,attempt);
assert.ok(ui.root.find(el=>el.textContent==="Complete the scene goal"));
const clearsBeforeSkip=sim.calls.prompts.length;
ui.skip();await flush();assert.equal(sim.calls.prompts.length,clearsBeforeSkip);assert.deepEqual(sim.calls.aborts,[]);assert.equal(sim.agent.get().brainActive,true);
assert.equal(sim.challenge.active.attempt_id,attempt);
assert.deepEqual(sim.calls.directives,["intro_agent"]);
ui.flow.destroy();ui=sim.mount();assert.equal(ui.flow.isActive(),false);ui.flow.destroy();
// Replaying after Skip still deliberately closes the existing attempt.
storage.clear();
const skippedReplay=simulator();let skippedUi=skippedReplay.mount();
skippedUi.choose("put_it_away");await flush();
const skippedToken=skippedReplay.challenge.active.attempt_id;
skippedUi.skip();await flush();assert.equal(skippedReplay.agent.get().brainActive,true);
startFirstRun();await flush();
assert.deepEqual(skippedReplay.calls.aborts,[skippedToken]);
assert.equal(skippedReplay.agent.get().brainActive,false);
assert.equal(readFirstRun().phase,"choosing");skippedUi.flow.destroy();
// Skip before any selection must never abort another browser's active mission.
storage.clear();const other=simulator();other.emitChallenge({list:FIRST_MISSIONS,active:{id:"put_it_away",state:"passed"}});
ui=other.mount();assert.equal(ui.flow.isActive(),true);ui.skip();await flush();assert.equal(other.calls.aborts.length,0);ui.flow.destroy();
// Skip while the environment is loading: no delayed start can rebuild the scene.
storage.clear();const slow=simulator();slow.session.switchEnvironment=id=>slow.calls.switches.push(id);
ui=slow.mount();ui.choose("way_out");await flush();ui.skip();await flush();
slow.emitEnvironment({environment:{id:"backrooms"},switch:null});await flush();assert.equal(slow.calls.starts.length,0);ui.flow.destroy();
// Both page close and Skip preserve an activation already requested.
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
  assert.equal(pending.agent.get().brainActive,true);
  assert.deepEqual(pending.calls.aborts,[]);
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

// Outside the first run, the same panel follows the server's environment roster
// and opens directly with normal manual challenge controls on a fresh browser.
storage.clear();
const scoped=simulator();ui=scoped.mount(false);
ui.root.find(el=>el.className==="agent-challenge-toggle").click();
assert.ok(ui.root.find(el=>el.className==="agent-challenge-dock").classList.contains("open"));
scoped.emitEnvironment({environment:{id:"backrooms",display_name:"The Backrooms"},switch:null});
scoped.emitChallenge({list:[FIRST_MISSIONS[1]],active:null});
assert.ok(ui.root.find(el=>el.textContent==="Challenges · The Backrooms"));
assert.ok(ui.root.find(el=>el.textContent==="Find a way out"));
assert.equal(ui.root.find(el=>el.textContent==="Put it away"),undefined);
ui.root.find(el=>el.className==="challenge-item").click();
assert.equal(scoped.calls.starts.at(-1).id,"way_out");
ui.root.find(el=>el.textContent==="Abort").click();
assert.equal(scoped.calls.aborts.length,1);
scoped.emitChallenge({list:[],active:null});
assert.ok(ui.root.find(el=>el.textContent==="No challenges in this environment yet."));
ui.flow.destroy();
console.log("ok - first missions reuse the challenge panel; environment roster and empty state follow the scene");

// Replay is explicit. It returns to the chooser without starting a world, then
// initializes a new attempt only after a choice, even in the same environment.
storage.clear();
saveFirstRun({phase:"choosing"});
const replay=simulator();ui=replay.mount();ui.choose("put_it_away");await flush();
const firstAttempt=replay.calls.starts[0].attempt_id;
startFirstRun();startFirstRun();await flush();
assert.equal(readFirstRun().phase,"choosing");assert.equal(replay.calls.starts.length,1);
assert.deepEqual(replay.calls.aborts,[firstAttempt]);assert.equal(replay.agent.get().brainActive,false);
ui.flow.destroy();ui=replay.mount();await flush();
assert.equal(ui.flow.isActive(),true);assert.ok(ui.root.find(el=>el.dataset.mission==="put_it_away"));
ui.choose("put_it_away");await flush();assert.equal(replay.calls.starts.length,2);
assert.notEqual(replay.calls.starts[1].attempt_id,firstAttempt);
ui.flow.destroy();ui=replay.mount();await flush();assert.equal(ui.flow.isActive(),true);assert.equal(replay.calls.starts.length,2);
replay.emitChallenge({...replay.challenge,active:{...replay.challenge.active,state:"passed"}});
startFirstRun();await flush();assert.equal(replay.agent.get().brainActive,false);
ui.choose("way_out");await flush();assert.equal(replay.calls.switches.at(-1),"backrooms");
assert.equal(replay.calls.starts.at(-1).id,"way_out");ui.flow.destroy();
console.log("ok - replay stops the owned attempt, persists the chooser and starts a fresh selected challenge");

{
  saveFirstRun({phase:"choosing"});
  const replayPending=simulator();const activate=replayPending.agent.setDirective;let releaseReplay, delayed=false;
  replayPending.agent.setDirective=async id=>{
    if(id && !delayed) {delayed=true;await new Promise(resolve=>{releaseReplay=resolve;});}
    return activate(id);
  };
  ui=replayPending.mount();ui.choose("put_it_away");await flush();
  startFirstRun();await flush();assert.equal(replayPending.calls.starts.length,1);
  releaseReplay();await flush();
  assert.equal(readFirstRun().phase,"choosing");
  assert.deepEqual(replayPending.calls.directives,["intro_agent",""]);
  assert.equal(replayPending.calls.starts.length,1);ui.flow.destroy();
}
console.log("ok - replay drains late activation before reopening the chooser");

// The actual broker -> controller -> session path reuses the initial picker.
// Opening stops only the owned attempt; a choice starts a fresh mission.
const oldParent=window.parent, oldReferrer=document.referrer;
const replies=[];window.parent={postMessage:(data,origin)=>replies.push({data,origin})};document.referrer="https://broker.example/session";
saveFirstRun({phase:"choosing"});
const worlds=simulator();ui=worlds.mount();ui.choose("put_it_away");await flush();
const removePicker=installMissionPicker(()=>new Promise(resolve=>startFirstRun(resolve)));
function brokerMessage(data,origin="https://broker.example",source=window.parent) {
 const event=new Event("message");Object.assign(event,{data:{channel:"innate:first-mission:v1",...data},origin,source});window.dispatchEvent(event);
}
brokerMessage({type:"get-controls"});assert.equal(replies.pop().data.canOpenMissionPicker,true);
const command={type:"open-mission-picker",requestId:"1"};
brokerMessage(command,"https://foreign.example");brokerMessage(command,"https://broker.example",{});
for(const requestId of [null,"","a".repeat(129)])brokerMessage({...command,requestId});
brokerMessage({type:"start-environment",environment:"backrooms",requestId:"old"});
await flush();assert.equal(worlds.calls.aborts.length,0);
for (const mission of FIRST_MISSIONS) {
  const before=worlds.calls.starts.length, oldAttempt=readFirstRun().attemptId;
  const change={...command,requestId:mission.id};
  brokerMessage(change);brokerMessage(change);await flush();
  assert.equal(replies.at(-1).data.success,true);
  assert.ok(replies.every(r=>r.data.type!=="completed")); // reopening the chooser is not Skip
  assert.equal(worlds.calls.starts.length,before);
  assert.equal(worlds.calls.aborts.at(-1),oldAttempt);
  assert.equal(worlds.agent.get().brainActive,false);
  assert.equal(readFirstRun().phase,"choosing");
  assert.equal(ui.root.find(el=>el.className==="first-mission").hidden,false);
  brokerMessage({...change,requestId:`already-choosing-${mission.id}`});await flush();
  assert.equal(worlds.calls.starts.length,before);
  ui.flow.destroy();ui=worlds.mount();await flush(); // Reload stays at the picker.
  assert.equal(worlds.calls.starts.length,before);
  ui.choose(mission.id);await flush();
  assert.equal(worlds.calls.starts.length,before+1);
  assert.equal(worlds.calls.starts.at(-1).id,mission.id);
  assert.notEqual(readFirstRun().attemptId,oldAttempt);
  assert.equal(worlds.agent.get().brainActive,true);
  brokerMessage(change);await flush(); // Replayed request must not close the new mission.
  assert.equal(readFirstRun().phase,"playing");
}
// Keep the action busy while its old agent stops; reject concurrent requests.
const originalActivate=worlds.agent.setDirective;let unblock;
worlds.agent.setDirective=async id=>{if(!id)await new Promise(resolve=>{unblock=resolve;});return originalActivate(id);};
brokerMessage({...command,requestId:"slow"});await flush();
brokerMessage({type:"get-controls"});assert.equal(replies.at(-1).data.busy,true);
brokerMessage({...command,requestId:"concurrent"});await flush();
assert.equal(replies.at(-1).data.success,false);
assert.equal(replies.some(r=>r.data.requestId==="slow"),false);
unblock();await flush();assert.equal(replies.at(-1).data.success,true);
assert.equal(readFirstRun().phase,"choosing");assert.equal(worlds.agent.get().brainActive,false);
worlds.agent.setDirective=originalActivate;
ui.choose("put_it_away");await flush();
// The real state adapter absorbs service errors. A stale active heartbeat must
// still fail the request and leave the owned attempt available for a retry.
const abortsBeforeFailure=worlds.calls.aborts.length;
worlds.agent.setDirective=async()=>{};
brokerMessage({...command,requestId:"failure"});await flush();
assert.equal(replies.at(-1).data.success,false);
assert.equal(worlds.calls.aborts.length,abortsBeforeFailure);
assert.equal(ui.root.find(el=>el.className==="first-mission").hidden,true);
worlds.agent.setDirective=originalActivate;
brokerMessage({...command,requestId:"retry"});await flush();
assert.equal(replies.at(-1).data.success,true);assert.equal(readFirstRun().phase,"choosing");
ui.flow.destroy();
// A route with no mounted controller fails immediately, without deferred work.
brokerMessage({...command,requestId:"unmounted"});await flush();assert.equal(replies.at(-1).data.success,false);
removePicker();const count=replies.length;brokerMessage(command);await flush();assert.equal(replies.length,count);
window.parent=oldParent;document.referrer=oldReferrer;
console.log("ok - broker mission picker: trusted sender, all choices, reload, duplicate/concurrent requests and failed stop");
