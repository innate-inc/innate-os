// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Browser integration fixture. Run from /debug/plant-reward.html with no robot/ROS.
// Exercise the real studio subscription and reward; only the world and brain are faked.
import { createAgentStudio } from "../js/agent/agentStudio.js";

export async function testStoryReward(check) {
  const host = document.createElement("div");
  host.hidden = true;
  host.className = "agent-cockpit agent-sim";
  document.body.append(host);
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const noop = () => {};
  let onChallenge = noop;
  let speech = 0;
  const notices = [];
  const state = {
    currentDirective: "void_agent", activeSkills: new Set(),
    agents: [{id:"void_agent", name:"MARS", skills:[], prompt:"", source:"innate"}],
  };
  const agent = { get:()=>state, subscribe:()=>noop, setActiveSkills:noop };
  const panel = {
    setOffers:noop, setComposerAsk:noop, setDisplayName:noop,
    isBusy:()=>false, narrate:async()=>{}, addNotice:line=>notices.push(line),
  };
  const session = { onChallenge:cb=>{onChallenge=cb;return noop;}, onEnvironment:()=>noop };
  const opts = {
    lastLine:()=>"", spokenCount:()=>speech, cancelSkill:async()=>{},
    motionAt:()=>0, resetMotion:noop, recalledAt:()=>0, turnedAt:()=>0, overlay:()=>noop,
  };
  let studio = createAgentStudio(host, agent, session, panel, opts);
  const emit = (id, attempt, status) => onChallenge({active:{id,attempt_id:attempt,state:status},profile:{name:"MARS"}});
  try {
    emit("way_out", "running", "running");
    check("A running adventure does not award the plant", !document.querySelector('dialog[open]'));
    emit("way_out", "failed", "failed");
    check("A failed adventure does not award the plant", !document.querySelector('dialog[open]'));
    emit("other", "other", "passed");
    check("Other completed challenges do not award the plant", !document.querySelector('dialog[open]'));
    const attempt = `integration-${Date.now()}`;
    emit("way_out", attempt, "passed");
    const first = document.querySelector('dialog[open]');
    check("Passing way_out opens the reward and retains story mode", !!first && document.body.classList.contains('story-active'));
    emit("way_out", attempt, "passed");
    check("Repeated observer frames do not restart the reveal", document.querySelector('dialog[open]')===first);
    speech++;
    await wait(600);
    check("Robot speech cannot release the ending before collection", notices.length===0);
    document.querySelector('.plant-reward-take').click();
    await wait(1500);
    check("Collection releases the ending exactly once", notices.filter(n=>n.startsWith('First life collected')).length===1 && !document.body.classList.contains('story-active'));
    check("Desktop keeps the plant with the agent editor", !!host.querySelector('.agent-studio-dock > .plant-keepsake'));
    studio.setCompact(true);
    check("Mobile keeps the plant accessible outside the hidden editor", !!host.querySelector(':scope > .plant-keepsake'));
    studio.setCompact(false);
    emit("way_out", attempt, "passed"); await wait(600);
    check("A repeated pass cannot duplicate the graduation notice", notices.length===2);
    studio.destroy();
    studio = createAgentStudio(host, agent, session, panel, opts);
    emit("way_out", attempt, "passed");
    check("Remounting an already collected attempt does not replay", !document.querySelector('dialog[open]'));
    emit("way_out", `${attempt}-next`, "passed");
    check("A new successful attempt gets its own celebration", !!document.querySelector('dialog[open]'));
    document.dispatchEvent(new Event('innate:play-intro'));
    check("Restart closes the reward while waiting for the world to acknowledge abort", !document.querySelector('dialog[open]'));
    onChallenge({active:null}); await wait(650);
    check("Aborting or changing the challenge cancels the reward and its pending graduation", !document.querySelector('dialog[open]') && notices.length===2);
  } finally {
    studio.destroy(); host.remove();
  }
}
