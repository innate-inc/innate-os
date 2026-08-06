#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Collect paired TTS-reference / mic recordings for training the barge-in
echo model.

Drives the production TTS path end-to-end: publishes each sentence on
/brain/tts, captures /tts/ref_audio (the exact PCM sent to the speaker) and
records the arm-camera mic with arecord for the duration of the utterance.
Each utterance is saved as <out>/<NNNN>_{ref,mic}.raw plus a metadata json.

Run on the robot with the stack up but no directive active (the input manager
must not be holding the mic):

    source config/dds/setup_dds.zsh && source ros2_ws/install/setup.zsh
    python3 scripts/audio/collect_echo_pairs.py --out data/echo_pairs \
        --sentences scripts/audio/sentences.txt

Vary conditions between runs (speaker volume via the webapp, arm pose via
teleop) — the metadata records the volume so training can condition on it.
"""

import argparse
import base64
import json
import subprocess
import time
from pathlib import Path

import rclpy
from rclpy.node import Node
from std_msgs.msg import String

MIC_RATE = 24_000
MIC_DEV = "plughw:Light,0"
# Live barge-in captures at this gain during TTS (micro_input ducks to it);
# training data MUST be recorded in the same regime or the model fits a
# different transfer function than the one it will see.
TTS_MIC_PERCENT = 50


class Collector(Node):
    def __init__(self, out_dir: Path):
        super().__init__("echo_pair_collector")
        self.out = out_dir
        self.tts_pub = self.create_publisher(String, "/brain/tts", 10)
        self.create_subscription(String, "/tts/ref_audio", self._on_ref, 50)
        self.create_subscription(String, "/tts/is_playing", self._on_playing, 10)
        self.ref_chunks: list[bytes] = []
        self.ref_rate = 44100
        self.playing = False
        self.saw_end = False

    def _on_ref(self, msg: String):
        event = json.loads(msg.data)
        etype = event.get("e")
        if etype == "start":
            self.ref_chunks = []
            self.ref_rate = int(event.get("rate", 44100))
        elif etype == "pcm":
            self.ref_chunks.append(base64.b64decode(event["pcm"]))
        elif etype == "end":
            self.saw_end = True

    def _on_playing(self, msg: String):
        self.playing = msg.data.strip().lower() == "true"

    def _spin_until(self, predicate, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=0.1)
            if predicate():
                return True
        return False

    def get_volume(self) -> int | None:
        try:
            out = subprocess.run(
                ["amixer", "-c", "APE", "sget", "Master"], capture_output=True, text=True, timeout=5
            ).stdout
            return int(out.split("[")[1].split("%")[0])
        except Exception:
            return None

    def collect_one(self, idx: int, sentence: str) -> bool:
        self.saw_end = False
        self.ref_chunks = []

        mic_path = self.out / f"{idx:04d}_mic.raw"
        # record straight to the file — a stdout pipe nobody drains blocks
        # arecord after ~1.4s (64KB pipe at 48KB/s)
        with open(mic_path, "wb") as mic_file:
            rec = subprocess.Popen(
                ["arecord", "-D", MIC_DEV, "-f", "S16_LE", "-r", str(MIC_RATE), "-c", "1", "-t", "raw", "-q", "-"],
                stdout=mic_file,
                stderr=subprocess.PIPE,
            )
            t_rec_start = time.monotonic()
            time.sleep(0.3)  # let capture start before audio does

            self.tts_pub.publish(String(data=sentence))
            ok = self._spin_until(lambda: self.playing, 15.0)
            t_play = time.monotonic()
            if ok:
                ok = self._spin_until(lambda: self.saw_end and not self.playing, 120.0)
            if ok:
                time.sleep(0.5)  # capture the reverb tail
            rec.terminate()
            try:
                _, err = rec.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                rec.kill()
                _, err = rec.communicate()
        if not ok:
            print(f"  [{idx}] TTS never {'finished' if self.playing else 'started'} — skipping")
            mic_path.unlink(missing_ok=True)
            return False
        mic_pcm = mic_path.read_bytes()
        if len(mic_pcm) < MIC_RATE:  # < 0.5 s of audio: arecord failed
            print(f"  [{idx}] mic capture failed: {err.decode(errors='replace')[:200]}")
            mic_path.unlink(missing_ok=True)
            return False

        ref_pcm = b"".join(self.ref_chunks)
        stem = self.out / f"{idx:04d}"
        (stem.parent / f"{stem.name}_ref.raw").write_bytes(ref_pcm)
        meta = {
            "sentence": sentence,
            "mic_rate": MIC_RATE,
            "ref_rate": self.ref_rate,
            "volume_percent": self.get_volume(),
            "rec_lead_s": round(t_play - t_rec_start, 3),
            "timestamp": time.time(),
        }
        (stem.parent / f"{stem.name}_meta.json").write_text(json.dumps(meta, indent=1))
        print(f"  [{idx}] ok: ref {len(ref_pcm) // 2} samples, mic {len(mic_pcm) // 2} samples")
        return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--sentences", required=True, type=Path)
    ap.add_argument("--start-index", type=int, default=None, help="default: continue after existing files")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    sentences = [s.strip() for s in args.sentences.read_text().splitlines() if s.strip() and not s.startswith("#")]
    if args.limit:
        sentences = sentences[: args.limit]
    args.out.mkdir(parents=True, exist_ok=True)
    start = args.start_index
    if start is None:
        existing = sorted(args.out.glob("*_meta.json"))
        start = int(existing[-1].name.split("_")[0]) + 1 if existing else 0

    subprocess.run(["amixer", "-q", "-c", "Light", "sset", "Mic", f"{TTS_MIC_PERCENT}%"], check=True)
    rclpy.init()
    node = Collector(args.out)
    ok = 0
    try:
        for i, sentence in enumerate(sentences):
            print(f"[{i + 1}/{len(sentences)}] «{sentence[:60]}…»")
            if node.collect_one(start + i, sentence):
                ok += 1
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        subprocess.run(["amixer", "-q", "-c", "Light", "sset", "Mic", "100%"])
        print(f"collected {ok} pairs into {args.out}")
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
