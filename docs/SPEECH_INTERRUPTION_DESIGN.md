# Speech Interruption (Barge-In) — Design Notes

**Status:** proposal / research summary. No code changes yet.

The goal: let a person interrupt MARS mid-sentence, have the robot stop talking, and have
it answer what was actually said to it. Today none of that is possible — the microphone is
switched off while the robot speaks.

This document records why the obvious fix (acoustic echo cancellation) does not work on our
hardware, what the alternatives are as of 2026, and a staged plan.

---

## Contents

- [Where we are today](#where-we-are-today)
- [Why AEC failed here](#why-aec-failed-here)
- [The option space](#the-option-space)
- [The transcript-diff idea](#the-transcript-diff-idea)
- [LiveKit adaptive interruption handling](#livekit-adaptive-interruption-handling)
- [Proposed plan](#proposed-plan)
- [Measure this first](#measure-this-first)
- [Open questions](#open-questions)
- [References](#references)

---

## Where we are today

The current speech path, end to end:

```
mic (arecord, 24 kHz mono, Arducam USB)
  -> workspace/inputs/micro_input.py: audio_loop      [DUCKED while the robot speaks]
  -> OpenAI Realtime WS (transcription-only, server_vad)
  -> /brain/chat_in -> cloud brain -> /brain/chat_out
  -> transport/chat.py: ChatManager.emit -> tts_handler.speak_text_async
  -> transport/tts.py: Cartesia sonic-3.5 -> aplay      [/tts/is_playing = "true"]
```

Two things in that path make barge-in impossible, independent of any acoustics work:

**1. Microphone audio is discarded while the robot talks.**
[`micro_input.py:286`](../workspace/inputs/micro_input.py#L286) drops every chunk instead of
forwarding it to the transcriber:

```python
# Skip sending while ducking (robot is speaking)
if self._is_robot_talking:
    ...
    continue
```

`_is_robot_talking` is driven by the `/tts/is_playing` topic. This is a deliberate echo
workaround — with the mic open, the transcriber would hear the robot and talk to itself —
but it means the user's interrupting speech never reaches any recognizer at all.

**2. There is no way to stop speech once it starts.**
[`TTSHandler`](../ros2_ws/src/brain/brain_client/brain_client/transport/tts.py) has no
`stop()`/`cancel()`. `speak_text` blocks on `aplay` until the utterance finishes, and a new
utterance arriving mid-playback is silently dropped
([`tts.py:135`](../ros2_ws/src/brain/brain_client/brain_client/transport/tts.py#L135)):

```python
with self.play_lock:
    if self.is_playing:
        self.logger.debug("🔊 Audio already playing, skipping new speech request")
        return False
```

So even a perfect interruption detector would have nothing to call. Fixing (2) is a
prerequisite for every option below and is worth doing on its own merits.

### The hardware, and why it is unkind

- **Speaker:** MAX98357A I2S amp on the Jetson's Tegra APE card (`config/alsa/asound.conf`),
  kept permanently clocked by `speaker-keepalive.service`.
- **Microphone:** the Arducam USB camera's built-in UAC mic (`scripts/update/setup_arducam.sh`,
  ALSA card "Light"). A single cheap capsule, no array, no beamforming, mounted on the same
  chassis as the speaker.
- ALSA does `dmix` + `softvol` only — no AEC plugin anywhere in the stack.

One omnidirectional capsule a short distance from a loudspeaker on a shared rigid body is
close to the worst case for echo: strong airborne coupling plus structure-borne vibration.

---

## Why AEC failed here

This is a known, quantified regime, not a tuning mistake.

A 2024 study of exactly this problem on the Pepper robot
([arXiv 2405.13477](https://arxiv.org/pdf/2405.13477)) measured the signal-to-noise ratio of
an interrupting human relative to the robot's own speech at Pepper's embedded mic:
**-22.33 dB (SD 4.09)**. The paper notes that classical target-speech-extraction systems are
built for a target SNR "no less than -5 dB". We are asking the algorithms to work roughly
17 dB outside their design envelope, on a single channel.

The same constraint shows up in vendor documentation for hardware AEC. XMOS's tuning guide
for the XVF3800 (the DSP behind the reSpeaker arrays) states that if the microphone signal
becomes **louder than 6 dB below the reference signal**, the adaptive filter can converge to
frequency-domain coefficients greater than 0 dB, with "a significantly negative effect on
algorithmic performance". Our mic is nowhere near 6 dB below the speaker; buying a mic array
does not by itself escape this, because the precondition is about coupling, not about the
number of capsules.

The general shape of the problem: linear adaptive filters model the speaker→mic path as a
linear impulse response. At high coupling, the small plastic speaker is driven into nonlinear
distortion and the chassis resonates, so a large fraction of the echo is simply not
predictable by a linear filter. What is left over (residual echo) is louder than the person
trying to talk.

---

## The option space

Ranked roughly by how well each survives our coupling.

| Approach | Handles -20 dB coupling? | Self-hostable | Effort | Evidence |
|---|---|---|---|---|
| **Learned TTS-reference subtraction** (Pepper pipeline) | Yes — demonstrated at exactly this SNR | Yes, local model | Medium-high | 14.4% WER on recovered speech vs 138% unfiltered |
| **Recognition-gated trigger** (ASR content, not energy) | Complementary — kills false triggers | Yes | Low | Two granted patents, 25 years of IVR practice |
| **Neural AEC** (DTLN-aec + MS AEC Challenge data) | Unproven at this SNR | Yes, runs on a Pi | Medium | Strong on normal-coupling benchmarks |
| **Hardware AEC** (XVF3800 / reSpeaker) | Only after physical decoupling | Yes | Low code, needs hardware + rework | Vendor-documented, with the 6 dB precondition |
| **Classical AEC** (WebRTC AEC3, Speex) | No — already tried | Yes | — | Failed on our robot |
| **LiveKit adaptive interruption** | Does not address echo at all | No, cloud-only | — | Solves a different sub-problem |
| **Full-duplex speech LLM** (Moshi-class) | Architecturally, but replaces the brain | Yes | Very high | Research-stage for robots |

### Learned TTS-reference subtraction — the strongest match

This is the Pepper pipeline, and it is the closest published system to our situation
(single embedded mic, social robot, human interrupting mid-utterance). The core idea is the
same instinct as our transcript-diff proposal — *the robot knows exactly what it is saying,
so use that as the reference* — but applied in the spectrogram domain rather than to text:

1. Take the TTS audio we are about to play (we have it: Cartesia returns the WAV before
   `aplay` gets it).
2. A small CNN predicts the spectrogram of *how that audio will sound coming back into our
   mic* — through our speaker, our chassis, our room.
3. Spectrally subtract that prediction from the live mic spectrogram.
4. Run normal STT on what remains.

Step 2 is the part that matters. The paper is explicit that subtracting the raw TTS audio
directly does not work, "due to the inconsistent and non-linear response of the speaker and
microphone" — which is the same reason linear AEC fails for us. The CNN learns our specific
nonlinear path instead of assuming a linear one.

Reported results: **14.43% mean WER** (7.69% median) on the extracted interrupting speech,
against **138.01% WER** for the unfiltered overlapping audio (i.e. unusable), beating
retrained VoiceFilter (19.53%) and ConvTasNet (23.53%).

Why this is attractive for us specifically:

- No new hardware.
- Training data is free and self-generating: have MARS speak a corpus to itself and record
  the mic. Paired (TTS audio, mic recording) data, collected unattended overnight.
- It is per-robot-configuration, which is fine — we control the hardware.

Caveats: authors' own dataset, arXiv preprint, baselines are 2018–2019 models. The paper
describes itself as "near-real-time"; whether the CNN plus STT fits our Jetson budget
alongside everything else is unverified and is the first thing to find out.

### Recognition-gated triggering

Independent of *how* we clean the audio, the decision "was that an interruption?" should be
gated on linguistic content rather than energy. Lucent's
[US6574595B1](https://patents.google.com/patent/US6574595B1) (filed 2000, now expired) made
this argument for IVR barge-in: energy detectors are "susceptible to falsely turning off the
prompt", while requiring recognizable sub-word content rejects coughs, breaths and
throat-clearing because "they will be modeled as contentless phonemes".

On a robot this matters more than on a phone line — MARS's own motors, fans and footsteps
are exactly the kind of broadband noise that trips an energy VAD. Any residual echo that
survives step 3 above is also non-speech-like, so a content gate suppresses it too.

### Neural AEC

[DTLN-aec](https://github.com/breizhn/DTLN-aec) is open source, real-time, 1.8M–10.4M
parameters, and has been demonstrated on a Raspberry Pi ([PiDTLN](https://github.com/SaneBow/PiDTLN)).
Microsoft's [AEC Challenge](https://github.com/microsoft/AEC-Challenge) repo open-sources
large training sets from 10,000+ real devices covering double-talk (i.e. barge-in), and the
ICASSP 2023 edition constrained entries to 20 ms algorithmic latency.

Being a neural model, it can represent the nonlinear speaker path that defeats AEC3. But it
still consumes the far-end reference signal, and **there is no published evidence of any of
these models working at -20 dB-class coupling** — the challenge data is normal device audio.
Worth a fine-tune on robot-recorded pairs as a second experiment; not worth betting the
feature on.

(One number to avoid repeating: a claimed "+0.30 MOS over the AEC Challenge baseline" for
DTLN-aec did not survive verification against the source. Don't cite it.)

### Hardware AEC and physical mitigation

A reSpeaker XVF3800-class array does AEC, beamforming, dereverberation and VAD on-DSP with a
hardware-synchronised reference, which removes a whole class of alignment bugs. It is a
reasonable parallel track — but per the 6 dB rule above, it is downstream of physically
improving the coupling: increasing speaker/mic separation, isolating the mic capsule from the
chassis mechanically, aiming the speaker away from the mic, and sane gain staging.

Physical mitigation helps *every* option on this list, including the CNN, and is the cheapest
thing on it. Note that moving the mic off the Arducam also decouples audio from the camera,
which is independently useful.

### Full-duplex speech models

Moshi and similar models handle overlap architecturally — the user's audio and the model's own
audio are parallel token streams with no explicit turn boundaries, so interruption emerges
natively. This is interesting long-term but replaces the entire brain, and does not by itself
solve self-hearing on a robot chassis. Worth noting that a 2025 LLM-based full-duplex dialogue
system ([arXiv 2502.14145](https://arxiv.org/pdf/2502.14145)) still puts conventional AEC as
the *first* module of its pipeline: the semantic layer classifies interruptions, but something
still has to remove the robot's own voice first. That is our bottleneck, and no amount of
cleverness further down the pipeline removes it.

---

## The transcript-diff idea

The proposal was: keep the mic open, transcribe continuously, and compare the transcript
against the text we are currently speaking. Divergence implies someone else is talking.

**The instinct is right and has real prior art.** Google's
[US9240183B2](https://patents.google.com/patent/US9240183) (filed 2014, granted 2016, still
active) covers precisely "use the known TTS output as a reference to suppress it during
recognition", explicitly for barge-in during TTS playback.

**But the patent does it one level below final text**, and that difference is the interesting
part. It extracts phoneme-weight vectors from both the TTS reference and the mic signal, then
down-weights the speech units common to both — suppression inside the recognizer's feature
stream, not a string comparison at the end. The claim language is about "identifying one or
more speech units that are present in the first set of vectors and the second set of vectors"
and marking them as "candidates for being suppressed".

Working at the text layer instead has three problems we should expect:

1. **Latency.** We inherit the full STT turnaround before we can even begin to decide whether
   to stop talking. Our transcripts arrive on
   `conversation.item.input_audio_transcription.completed` — after OpenAI's server VAD has
   decided the turn ended (`silence_duration_ms: 700`). That is far too late to stop a
   sentence naturally; people expect the robot to stop within a few hundred milliseconds.
2. **The recognizer hears the robot, not the human.** At -22 dB the transcriber will lock onto
   the loud, clean, dominant signal — our own Cartesia output — and the quiet human may not
   appear in the transcript at all. A diff cannot detect what was never transcribed.
3. **STT errors look exactly like interruptions.** Whisper-class and streaming models
   mis-transcribe and hallucinate on degraded audio. Every such error is a spurious
   "divergence" and therefore a spurious interruption. The signal we want (a stranger's words)
   and the noise (our own words, transcribed badly) live in the same channel with no way to
   tell them apart.

The honest version of the idea is therefore: **keep the "use the known TTS as a reference"
insight, but apply it in the audio/feature domain, where the SNR problem is actually solved,
rather than in the text domain, where it is merely observed.** That is the Pepper pipeline.

That said, (2) and (3) are empirical, and the experiment is cheap — see
[Measure this first](#measure-this-first). If it turns out our STT does track the robot's
script faithfully at our real coupling, a text-domain diff is a legitimate stopgap detector
while the CNN work happens, and it needs almost no new infrastructure.

One non-technical note: US9240183B2 is **active**. If we productise a feature-domain
self-speech suppressor, that is worth a look from legal. The expired Lucent patent covers the
recognition-gating half.

---

## LiveKit adaptive interruption handling

Worth understanding precisely, because it is easy to over-read what it does.

LiveKit's [adaptive interruption handling](https://docs.livekit.io/agents/logic/turns/adaptive-interruption-handling/)
is a **post-VAD acoustic classifier**: an audio encoder plus a CNN, trained on real
conversational audio, which — after VAD has flagged incoming speech — decides whether that
speech is a genuine barge-in or a backchannel ("uh-huh", "right", "okay"). It works on
waveform shape, onset sharpness, duration and prosody rather than waiting for a transcript.
LiveKit self-reports 86% precision and 100% recall at 500 ms of overlap, and says it rejects
51% of VAD-triggered false interruptions. (Vendor-reported, internal held-out set, no
third-party validation.)

Two things follow:

- **It does not solve our problem.** It assumes the audio reaching it is already the user's
  voice. There is no echo cancellation or self-speech separation in it. Pointed at our mic, it
  would be classifying our own TTS.
- **We cannot host it.** It runs only on LiveKit Cloud's inference infrastructure; self-hosted
  deployments fall back to plain VAD interruption
  ([livekit/agents#6033](https://github.com/livekit/agents/issues/6033) tracks the request).
  Their separate *end-of-turn* detector is open-weights
  ([livekit/turn-detector](https://huggingface.co/livekit/turn-detector), a Qwen2.5-0.5B
  fine-tune), but that is turn detection, not barge-in.

The architecture is still worth copying at stage 4 — once self-speech is suppressed, we will
want exactly this classifier so that "mm-hm" does not stop the robot mid-sentence.

---

## Proposed plan

Staged so that each step is independently useful and the risky part comes after the
measurement that justifies it.

**Stage 0 — make interruption expressible at all.** Add `stop()` to `TTSHandler`: kill the
in-flight `aplay`, drain the `_speech_queue`, publish `/tts/is_playing false` promptly. Relax
the drop-if-playing guard at `tts.py:135` so a newer utterance can supersede an older one.
Wire a cancel into `ChatManager.emit` and `_on_chat_in`. This is worth landing regardless of
which detection approach wins — today a skill that says something long cannot be shut up, and
`/brain/chat_in` arriving during speech is dropped on the floor. Note there is currently no
test coverage for the TTS queue (`test_tts_queue.py` is gone, only a stale `.pyc` remains), so
this stage should bring its own tests.

**Stage 1 — measure.** See below. Do not build anything else before these numbers exist.

**Stage 2 — learned TTS-reference subtraction.** Collect paired (TTS audio, mic recording)
data on a real MARS overnight, train the CNN, spectral-subtract, feed the residual to the
existing OpenAI Realtime transcription path. Replace the ducking `continue` at
`micro_input.py:286` with "send the cleaned residual" rather than "send nothing".

**Stage 3 — recognition-gated trigger.** Only treat the residual as an interruption if it
contains plausible linguistic content. Prevents motor noise, fans and residual echo from
stopping the robot.

**Stage 4 — backchannel classification.** LiveKit-style: distinguish a real interruption from
"mm-hm". Only meaningful once stages 2–3 produce a clean user-only signal.

**In parallel, throughout — physical mitigation.** Separation, isolation, gain staging, and
evaluating a dedicated mic (array or otherwise) that is not bolted to the camera. This
improves the input to every stage above and may be the highest value-per-hour work on the
list.

---

## Measure this first

Three cheap experiments, all on a real MARS, before committing to any implementation:

1. **Our actual coupling ratio.** Play a known utterance at normal volume, have a person speak
   at a normal conversational distance and level, record the mic, compute the SNR of the human
   relative to the robot. The Pepper figure of -22.33 dB is *their* hardware. Ours could be
   worse (smaller chassis, mic closer) or better. This single number determines which options
   on the list are even in play, and it is an afternoon of work.

2. **Does the STT transcribe our own speech reliably?** Remove the ducking guard locally, let
   the mic feed reach the transcriber while the robot talks, and compare the returned
   transcript against the Cartesia input text. If it tracks the script closely, the
   transcript-diff stopgap is viable. If it hallucinates or drops words, that settles it, and
   we have learned it for the price of deleting three lines.

3. **Can a human be heard at all?** With the mic open during TTS, have someone interrupt and
   inspect the raw recording — spectrogram, and what the transcriber returns. This tells us
   whether there is any recoverable signal for a CNN to extract, which is the assumption stage
   2 rests on.

---

## Open questions

- Does the CNN plus spectral subtraction plus STT fit the Jetson's real-time budget alongside
  navigation, vision and the arm? The paper claims "near-real-time" on unspecified hardware.
- How much paired data does the CNN need to converge for our speaker/mic/enclosure, and does
  it need retraining per robot unit, or does one model generalise across the fleet?
- Does volume ducking (playing quieter rather than stopping) buy enough SNR to make simpler
  approaches viable? Nobody in the literature seems to have measured the trade-off between
  intelligibility of the robot and recoverability of the human.
- How should an interruption propagate to the cloud brain? There is no chat-cancel message
  type today — `message_types.py` has only `PRIMITIVE_INTERRUPTED`. An interrupted utterance
  probably needs to be reflected in conversation history as "said only the first N words", or
  the brain's model of what the robot has told the user drifts from reality.
- Freedom-to-operate on US9240183B2 for a feature-domain suppressor.

---

## References

Verified sources behind the claims above.

- **Pepper ego-speech filtering pipeline** — [arXiv 2405.13477](https://arxiv.org/pdf/2405.13477).
  The -22.33 dB measurement, the CNN + spectral subtraction method, the WER results.
- **Google, reference signal suppression in speech recognition** —
  [US9240183B2](https://patents.google.com/patent/US9240183). Phoneme-domain suppression of
  known TTS output for barge-in. Active.
- **Lucent, sub-word barge-in detection** — [US6574595B1](https://patents.google.com/patent/US6574595B1).
  Recognition-gated triggering, and why energy detectors false-trigger. Expired.
- **DTLN-aec** — [github.com/breizhn/DTLN-aec](https://github.com/breizhn/DTLN-aec),
  [PiDTLN](https://github.com/SaneBow/PiDTLN) for embedded deployment.
- **Microsoft AEC Challenge** — [github.com/microsoft/AEC-Challenge](https://github.com/microsoft/AEC-Challenge).
  Open double-talk training data; 2023 edition's 20 ms latency constraint.
- **XMOS XVF3800 tuning guide** —
  [xmos.com](https://www.xmos.com/documentation/XM-014888-PC/html/modules/fwk_xvf/doc/user_guide/04_tuning_the_application.html).
  The 6 dB mic-below-reference precondition.
- **LiveKit adaptive interruption handling** —
  [docs](https://docs.livekit.io/agents/logic/turns/adaptive-interruption-handling/),
  [engineering blog](https://livekit.com/blog/adaptive-interruption-handling),
  [self-hosting request](https://github.com/livekit/agents/issues/6033).
- **Full-duplex LLM dialogue system** — [arXiv 2502.14145](https://arxiv.org/pdf/2502.14145).
  Still uses conventional AEC as its first pipeline stage.
- **Robot turn-taking with self-monitoring VAP** — [arXiv 2501.08946](https://arxiv.org/html/2501.08946v1).
  Feeds the robot's own TTS into the turn-taking model as context.

Areas the survey did not cover with verified sources, and which remain genuinely open:
Pipecat/Vocode/OpenAI Realtime/Gemini Live/Nova Sonic internals, speaker diarization and
target-speaker VAD, and contact microphones.
