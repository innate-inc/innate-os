// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Exercise the rendered tour and Help replay with desktop/phone panel geometry.
import assert from 'node:assert/strict';
import {createInterfaceTour} from '../js/uiTour.js';
import {ONBOARDING_REQUEST_EVENT} from '../js/onboarding.js';

const rect = (left, top, width, height) => ({left, top, width, height, right:left+width, bottom:top+height});
class Element extends EventTarget {
  children=[]; dataset={}; style={}; isConnected=true; textContent='';
  bounds=rect(0,0,0,0);
  classList={values:new Set(),add:name=>this.classList.values.add(name),remove:name=>this.classList.values.delete(name),contains:name=>this.classList.values.has(name)};
  append(...children) {this.children.push(...children);for(const child of children)child.parent=this;}
  remove() {this.isConnected=false;if(this.parent)this.parent.children=this.parent.children.filter(c=>c!==this);}
  setAttribute() {}
  focus() {document.activeElement=this;}
  getBoundingClientRect() {return this.className==='ui-tour-card'?rect(parseFloat(this.style.left)||0,parseFloat(this.style.top)||0,330,210):this.bounds;}
  find(text) {return this.children.find(c=>c.textContent===text)||this.children.map(c=>c.find(text)).find(Boolean);}
  click() {this.dispatchEvent(new Event('click'));}
}
globalThis.HTMLElement=Element;
globalThis.document=Object.assign(new EventTarget(),{body:new Element(),createElement:()=>new Element(),documentElement:{clientWidth:1440},activeElement:null});
globalThis.window=Object.assign(new EventTarget(),{visualViewport:Object.assign(new EventTarget(),{width:1440,height:900,offsetLeft:0,offsetTop:0})});
globalThis.innerWidth=1440;globalThis.innerHeight=900;
globalThis.getComputedStyle=el=>({visibility:el.hidden?'hidden':'visible',display:'block'});
let observer;
globalThis.ResizeObserver=class {constructor(cb){this.callback=cb;this.targets=new Set();observer=this;}observe(el){this.targets.add(el);}unobserve(el){this.targets.delete(el);}disconnect(){this.targets.clear();}};
const root=new Element();root.bounds=rect(64,0,1376,900);
const composer=new Element();composer.classList.add("agent-compose");composer.bounds=rect(1040,770,360,90);
const controls=new Element();controls.classList.add("agent-control-panel");controls.bounds=rect(1020,20,400,115);
const header=new Element();header.classList.add('agent-sheet-header');header.hidden=true;
const cameras=new Element();cameras.bounds=rect(84,20,240,150);
const help=new Element();help.bounds=rect(12,820,40,40);
const targets={'.agent-compose':[composer],'.agent-control-panel':[controls],'.agent-sheet-header':[header],'.overlay-stack-top-left':[cameras],'.rail-help':[help]};
document.querySelectorAll=selector=>targets[selector]||[];
help.addEventListener('click',()=>window.dispatchEvent(new Event(ONBOARDING_REQUEST_EVENT)));
const tour=createInterfaceTour(root,'agent');
const card=()=>document.body.children.find(el=>el.className==='ui-tour-card');
const next=()=>card().find('Next').click();
function clearOf(target) {
 const c=card().getBoundingClientRect(),t=target.bounds;
 assert.ok(c.right<=t.left||c.left>=t.right||c.bottom<=t.top||c.top>=t.bottom,'card must not cover its target');
 assert.ok(c.left>=16&&c.top>=16&&c.right<=window.visualViewport.width-16&&c.bottom<=window.visualViewport.height-16,'card must fit viewport');
}
help.click();assert.ok(card().find('Talk to MARS'));clearOf(composer);assert.equal(card().dataset.placement,'left');
next();assert.ok(card().find('Choose how MARS thinks'));clearOf(controls);assert.equal(card().dataset.placement,'left');
help.click();assert.ok(card().find('Quick tour · 1 / 4'));assert.equal(document.body.children.length,1);
next();next();next();card().find('Done').click();assert.equal(card(),undefined);
help.click();assert.ok(card().find('Quick tour · 1 / 4'));
console.log('ok - Help restarts after completion and mid-tour; desktop cards sit beside their controls');

// A phone has no room beside the panel: anchor above the composer, then below
// the controls. A collapsed sheet swaps to the visible header on resize.
root.classList.add("agent-compact");window.visualViewport.width=390;window.visualViewport.height=844;document.documentElement.clientWidth=390;globalThis.innerWidth=390;globalThis.innerHeight=844;
composer.bounds=rect(30,725,330,65);controls.bounds=rect(16,420,358,90);
window.dispatchEvent(new Event('resize'));clearOf(composer);assert.equal(card().dataset.placement,'top');
next();clearOf(controls);
composer.hidden=true;controls.hidden=true;header.hidden=false;header.bounds=rect(16,760,358,60);
observer.callback();clearOf(header);assert.ok(card().find("Start or stop MARS from this bar. Open chat to choose an agent and see its controls."));assert.ok(header.classList.contains('ui-tour-target'));assert.ok(!controls.classList.contains('ui-tour-target'));
window.visualViewport.height=420;header.bounds=rect(16,350,358,54);window.visualViewport.dispatchEvent(new Event('resize'));clearOf(header);
const escape=new Event('keydown',{cancelable:true});Object.defineProperty(escape,'key',{value:'Escape'});document.dispatchEvent(escape);assert.equal(card(),undefined);assert.equal(observer.targets.size,0);
root.classList.add('agent-conversation-onboarding');help.click();assert.equal(card(),undefined);root.classList.remove('agent-conversation-onboarding');
help.click();assert.ok(card());tour.destroy();help.click();assert.equal(card(),undefined);
console.log('ok - mobile resize, collapsed sheet, keyboard close, first-mission guard and teardown');
