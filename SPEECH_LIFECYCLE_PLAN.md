# Speech lifecycle portability correction (offline candidate)

Acceptance:
- Shared per-agent `listen_before_acting()` defaults false; Household opts in. No scene/apartment/name rules in shared runtime.
- Real Endpointer acoustic onset emits one start, silence closes into pending STT, and success/empty/error/discard/drop/reset retires a hold exactly once. No model event per PCM chunk.
- Preserve legacy completed transcript delivery for non-opted-in agents; no speaker/provenance promotion. Microphone remains operator input, resident playback remains environment input.
- Simulator acquires hold at TTS on_start, with activation/request context captured before enqueue. No known text delivered before successful completion; failed unheard clips are not transcripts.
- Stop/deactivation invalidates old request/activation tokens. Orphan holds have a 90s bound; late completions are discarded.
- Realtime vendor responses lack correlation IDs in existing client. Never shift FIFO after missing response: buffer locally closed PCM (up to three pending utterances) and send one manual commit at a time; routine consecutive speech is retained. Actual failure/timeout invalidates the socket association and uses existing reconnect. Only overflow/failure text may be lost; legacy streaming path remains unchanged when disabled. This changes opted-in realtime STT to upload after local endpointing; no claim of equal STT latency.
- Own-TTS ducking remains; batch and realtime flush pre-duck utterance without feeding synthetic silence to VAD.

Source evidence: `inputs/batch_stt.py` Endpointer and BatchSttSession; `workspace/inputs/micro_input.py` manual commits/ducking; `inputs/manager.py` routes input; `core/lifecycle.py` already sends active input configuration; `brain/agent.py` owns dispatch gate and request generation; `nodes/brain_client_node.py` owns simulator playback callbacks.

Verification plan: focused actual generated PCM endpoint/pending/terminal tests with fake transcribers; duplicate/timeout/session fences; existing microphone/reconnect and incoming-speech/provider dispatch tests adapted to acoustic onset and opt-in; explicit Stop and non-opted-in regression checks. No recognition accuracy claim, paid providers, physical motion or ASR benchmark. Host tests: 150 passed, 14 ROS skips before the final ducking case. Native ROS: 164 passed in 5.26s before the final ducking case. No paid services or live robot/simulator started; only existing sleep-only test container. Final verification receipt is supplied with the commit.

Simulator architecture limit: its existing environment_speech path forwards original NPC text after TTS, bypassing ASR. This correction does not change that baseline architecture. Timing is playback callback onset, not a claim of physical acoustic synchronization.

Locking: realtime Endpointer, pending commit, lifecycle timer and socket identity use one shared RLock, including replacement and timeout invalidation; no opposite lifecycle/session lock order. Native test covers actual PCM through MicroInput, manager publication, BrainClientNode and BrainAgent, including contentless completion and a legacy duplicate crossing policy change.

Protocol reference checked 2026-09-06: https://elevenlabs.io/docs/eleven-api/guides/how-to/speech-to-text/realtime/transcripts-and-commit-strategies documents one stable segment after a manual commit, clearing accumulated transcript, and automatic commit around 36s of accumulated audio. Our existing Endpointer caps segments at 30s. https://elevenlabs.io/docs/eleven-api/guides/how-to/speech-to-text/realtime/event-reference distinguishes committed_transcript from optional later timestamp events; only the former advances our queue. No FIFO association across failures is assumed.

Final native focused lifecycle test: 13 passed in 0.47s after adding noise/own-TTS and real legacy duplicate publication; combined earlier native suite: 164 passed in 5.26s. These runs cover all final executable changes. Ruff and git diff --check pass. Test container stopped and inspected as exited / Running=false.
