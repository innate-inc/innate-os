import assert from "node:assert/strict";
import { createPromptSuggestions } from "../js/agent/promptSuggestions.js";
import { isInternalOnboardingSkill } from "../js/agent/chatStream.js";

const displayed=[];
const suggestions=createPromptSuggestions(prompts=>displayed.push(prompts));
const event=(status, prompts=["Try picking it up again"], primitive_id="run", timestamp=Date.now()/1000) => suggestions.consume({skill_id:"innate-os/suggest_user_prompts",primitive_id,status,timestamp,args:{prompts}});
event("completed"); assert.equal(displayed.length,0); // history/terminal alone
for (const status of ["failed","interrupted"]) {event("running");event(status);event("completed");}
assert.equal(displayed.length,0);
event("running");event("completed",["Pick it up", "Put it in the box"]);
assert.deepEqual(displayed.at(-1),["Pick it up", "Put it in the box"]);
event("completed");assert.equal(displayed.length,1); // duplicate terminal
event("running");suggestions.clear();event("completed");assert.equal(displayed.at(-1),null);assert.equal(displayed.length,2);
event("running",[],"stale",1);event("completed",[],"stale");assert.equal(displayed.length,2);
for(const prompts of [[""], [123], ["x".repeat(161)], ["a","b","c","d"]]) {event("running");event("completed",prompts);}
assert.equal(displayed.length,2);
event("running");event("completed",[]);assert.deepEqual(displayed.at(-1),[]);
assert.equal(suggestions.consume({skill_id:"innate-os/pick_any_object"}),false);
assert.ok(isInternalOnboardingSkill("suggest user prompts"));
assert.ok(isInternalOnboardingSkill("innate-os/suggest_user_prompts"));
console.log("ok - live suggestions: completion, optional choices, clear, stale/duplicate/malformed events and history filtering");
