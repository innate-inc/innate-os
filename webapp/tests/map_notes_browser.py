# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Real page + ROS service integration (fixture, never a physical robot).

Start brain_client/test/map_notes_fixture.py and rosbridge_websocket on 9090,
serve webapp with its /ws proxy, then:
  python webapp/tests/map_notes_browser.py --url http://localhost:8765 --chrome /path/to/chrome
Requires Playwright. The URL MUST point at the local fixture, which exposes
/test/change_map; this script verifies that endpoint before any writes.
"""

import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--chrome")
    parser.add_argument("--screenshots", type=Path)
    args = parser.parse_args()
    with sync_playwright() as p:
        browser = p.chromium.launch(**({"executable_path": args.chrome} if args.chrome else {}))
        page = browser.new_page(viewport={"width": 1400, "height": 950})
        errors = []
        sent = []
        page.on("websocket", lambda ws: ws.on("framesent", lambda frame: sent.append(json.loads(frame))))
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(args.url + "/nav")
        page.evaluate(
            "async()=>{const {ros}=await import('/js/rosClient.js');window.testRos=ros;ros.connect(location.hostname)}"
        )
        page.wait_for_function(
            "async()=>{try {const r=await testRos.callService('/test/reset_notes',{});return r.success;} catch {return false;}}",
            timeout=15000,
        )
        expect(page.get_by_role("button", name="Notes 3", exact=True)).to_be_visible(timeout=15000)
        # A test-only service proves this is our fixture, and exercises the no-map state.
        assert page.evaluate("async()=> (await testRos.callService('/test/change_map',{})).success")
        expect(page.get_by_role("button", name="Notes", exact=True)).to_be_visible()
        assert page.locator(".map-note-pin:visible").count() == 0
        page.evaluate("()=>testRos.callService('/test/change_map',{})")
        expect(page.get_by_role("button", name="Notes 3", exact=True)).to_be_visible()
        page.get_by_role("button", name="Notes 3", exact=True).click()
        page.locator(".map-note-panel").get_by_role("button", name="Blue bowl", exact=True).click()
        panel = page.locator(".map-note-panel")
        panel.get_by_role("button", name="View captured image", exact=True).click()
        expect(panel.locator("img")).to_be_visible()
        assert panel.locator("img").evaluate("img=>img.naturalWidth") > 0
        panel.get_by_role("button", name="Edit", exact=True).click()
        page.get_by_role("textbox", name="Note text", exact=True).fill("On the top kitchen shelf.")
        panel.get_by_role("button", name="Save note", exact=True).click()
        expect(panel).to_contain_text("On the top kitchen shelf.")
        expect(panel.get_by_role("button", name="Edit", exact=True)).to_be_visible()
        # A second writer updates the same revision while the browser editor is open.
        panel.get_by_role("button", name="Edit", exact=True).click()
        page.get_by_role("textbox", name="Note text", exact=True).fill("Stale operator edit")
        result = page.evaluate("""async()=>{
          const snap=JSON.parse((await testRos.callService('/brain/map_notes',{request:JSON.stringify({operation:'snapshot'})})).response);
          const note=snap.notes.find(n=>n.title==='Blue bowl');
          return JSON.parse((await testRos.callService('/brain/map_notes',{request:JSON.stringify({operation:'write_map_note',map_ref:snap.map_ref,request_id:crypto.randomUUID(),arguments:{note_id:note.id,expected_revision:note.revision,title:note.title,text:'Concurrent agent correction',certainty:'observed',observation_id:null}})})).response);
        }""")
        assert result["ok"]
        panel.get_by_role("button", name="Save note", exact=True).click()
        expect(panel).to_contain_text("REVISION_CONFLICT")
        expect(page.get_by_role("textbox", name="Note text", exact=True)).to_have_value("Stale operator edit")
        panel.get_by_role("button", name="Cancel", exact=True).click()
        expect(panel).to_contain_text("Concurrent agent correction")
        # Restore the fixture note and verify creation by clicking the real map canvas.
        panel.get_by_role("button", name="Edit", exact=True).click()
        page.get_by_role("textbox", name="Note text", exact=True).fill("On the kitchen table. Seen from here.")
        panel.get_by_role("button", name="Save note", exact=True).click()
        expect(panel.get_by_role("button", name="All notes", exact=True)).to_be_visible()
        panel.get_by_role("button", name="All notes", exact=True).click()
        panel.get_by_role("button", name="Add note", exact=True).click()
        canvas = page.locator(".map-canvas")
        canvas.click(position={"x": 350, "y": 400})
        page.get_by_role("textbox", name="Note title", exact=True).fill("Keys")
        page.get_by_role("textbox", name="Note text", exact=True).fill("Keys on the side table.")
        panel.get_by_role("button", name="Save note", exact=True).click()
        expect(page.get_by_role("button", name="Notes 4", exact=True)).to_be_visible()
        expect(panel).to_contain_text("Known map position")
        # Both surfaces share the same persisted store; navigating remounts the widget.
        page.goto(args.url + "/teleop")
        page.get_by_role("button", name="Switch to Map view · scroll to zoom", exact=True).click(timeout=15000)
        expect(page.get_by_role("button", name="Notes 4", exact=True)).to_be_visible(timeout=15000)
        page.get_by_role("button", name="Notes 4", exact=True).click()
        panel.get_by_role("button", name="Keys", exact=True).click()
        expect(panel).to_contain_text("Keys on the side table.")
        panel.get_by_role("button", name="Remove", exact=True).click()
        expect(page.get_by_role("button", name="Notes 3", exact=True)).to_be_visible()
        page.reload()
        expect(page.get_by_role("button", name="Notes 3", exact=True)).to_be_visible(timeout=15000)
        page.goto(args.url + "/nav")
        expect(page.get_by_role("button", name="Notes 3", exact=True)).to_be_visible(timeout=15000)
        page.get_by_role("button", name="Notes 3", exact=True).click()
        panel.get_by_role("button", name="Blue bowl", exact=True).click()
        if args.screenshots:
            args.screenshots.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(args.screenshots / "map-scratchpad.png"))
        # A small screen must expose the full editor and its Save button without overflow.
        page.set_viewport_size({"width": 390, "height": 844})
        panel.get_by_role("button", name="Edit", exact=True).click()
        expect(panel.get_by_role("button", name="Save note", exact=True)).to_be_in_viewport()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if args.screenshots:
            page.screenshot(path=str(args.screenshots / "map-scratchpad-mobile.png"))
        page.goto(args.url + "/teleop")
        expect(page.get_by_role("button", name="Notes 3", exact=True)).to_be_visible(timeout=15000)
        page.get_by_role("button", name="Notes 3", exact=True).click()
        panel.get_by_role("button", name="Blue bowl", exact=True).click()
        panel.get_by_role("button", name="Edit", exact=True).click()
        expect(panel.get_by_role("button", name="Save note", exact=True)).to_be_in_viewport()
        assert not any(message.get("op") == "send_action_goal" for message in sent), "Note interaction dispatched a physical action"
        assert not errors, json.dumps(errors)
        browser.close()
        print(
            "Passed: map detach/restore, read evidence, edit, concurrent edit conflict, create by map click, Teleop read/remove, reload, mobile editor; no page errors."
        )


if __name__ == "__main__":
    main()
