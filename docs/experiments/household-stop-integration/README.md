# Household Stop integration

Based on grounded conversation candidate B `d3fe1d30578fb1f5bb955ab1481f611f36130e20`.
Ports draft Stop-request PR #781 through local SHA
`5edfde9bf600c37adf325661ad2f211ea548146a`, while retaining B grounding and A's
incoming-speech gate. Household intentional replanning passes `continue_task=true`;
operator Stop cancels the remaining request. Only a new operator receipt can
authorize `start_new_request`, followed by fresh action schemas on the next turn.

Operator/environment provenance and request generation are assigned locally at
receipt. NPC playback preserves its original generation; delayed transcripts and
neutral listening-cancellation results cannot authorize restart. Operator events
fenced by incoming speech remain pending, including input arriving between commit
and dispatch. Later operator receipts supersede earlier ones. A Stop issued by an
older in-flight turn still stops the robot, but preserves newer unseen operator
input in the new request generation for explicit restart on a fresh turn.

Independent review reproduced and verified fixes for gated WAIT consumption,
commit-to-act input arrival, and old Stop versus newer operator receipt ordering.
Both Gemini and native Responses host sequences cover these boundaries, explicit
Stop, temporary cancellation, provider outcome completion, and restart schemas.
Host result: **146 passed, 6 ROS-dependent skipped**. No simulator/provider calls
were made for these tests; this is mocked integration evidence, not a full-run
speed or completion claim.

Reviewed pre-documentation SHA-256 provenance:

- Full implementation diff: `ec5dbfb0fbe4d1610816fa5bf19f462a04d018b5575478e5874fa08bac29a864`
- `brain/agent.py`: `ee1630e0f3a506e0a7753f01b9d1b7012250f00853b8f5311b7088101f5babbd`
- `test/test_incoming_speech.py`: `162903e73a9157a3ba033fc8eedf3cbb8f5284ea031a5566bc42a21cca9ff093`
