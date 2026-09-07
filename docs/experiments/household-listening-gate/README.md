# Incoming speech gate: isolated candidate

Prepared September 6, 2026 on `codex/household-listening-gate`, from frozen baseline
`468b4c3366b7375bfc14e0daf06284e78499b0b1`. The frozen baseline measurement remains
pending. This candidate has not been deployed or evaluated with a paid model run;
there is no completion-rate, latency, cost, or performance improvement claim.

## Behavior and acceptance

The generic `BrainAgent` input lifecycle begins before simulated resident speech
enters the synthesis/playback queue. An opaque token identifies each utterance;
overlapping utterances keep the gate closed. Completion synchronously queues the
transcript before retiring its token. The brain receives no transcript early.

- While input is active, tools collapse to wait/stop and actual dispatch rejects
  new skills, including navigation and identity work. Each model turn captures an
  input generation, so a stale response remains fenced after playback finishes.
- Final speech eligibility, buffered audio, and robot chat publication share the
  input lock. A muted stale reply cannot reach the simulator as complete dialogue.
  Every returned tool call still receives its Gemini/native Responses outcome.
- Input arrival interrupts only agent-owned `navigate_to_position` and
  `find_next_person`, including goals awaiting acceptance. Normal result cleanup
  remains responsible for releasing the skill slot; the interrupted result asks
  the model to continue the request after consuming the transcript. It does not
  automatically restart navigation or invoke user Stop.
- Automatic interruption leaves manual and unrelated skills alone. A committed
  turn carrying user input retains the existing explicit Stop path. Robot-owned
  TTS does not start an input gate.
- Duplicate/late callbacks, reset, and deactivation cannot restore old input.
  Missing TTS, queue refusal/exception, terminal retry failure, and queue close
  release the gate through the completion path.

The existing ten-second interaction heuristic is unchanged. The observed reply
took approximately **12 seconds from receipt to delivery**, including queueing
and synthesis; **audible playback was approximately 9 seconds**. The deterministic
test uses a synthetic twelve-second WAV as a longer playback stress case. It is
not a measurement of the observed clip.

## Verification

**38 tests passed in the dedicated ROS test container** across
`test_incoming_speech.py`, `test_runner_deactivation.py`, and `test_tts.py`. These
exercise the actual node callback, TTS queue/worker and WAV-duration playback
path, plus runner acceptance/cancellation/result cleanup, using fake transport
and playback waits. No ROS nodes or provider calls were launched. The container
was stopped and verified `running=false` afterward.

Independent review then found a final-publication interleaving. A new deterministic
host test reproduced it before the fix and passed afterward. The final host run
was **105 passed, 6 ROS-only skipped** across incoming speech, local brain, and
OpenAI context tests. It includes transcript enqueue ordering, provider call-ID
completion, overlaps, explicit Stop, and both streaming/publication races. The
node and runner files were unchanged after their ROS run; that run predates the
final publication-lock adjustment in `agent.py`. Ruff and diff checks passed.

This is deterministic integration evidence with mocked providers, not physical
motion or full-challenge evidence. The live baseline checkout and runtime files
were left unchanged throughout candidate preparation.
