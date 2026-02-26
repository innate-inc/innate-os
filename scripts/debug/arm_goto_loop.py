#!/usr/bin/env python3
"""Loop through arm poses with configurable delays. Edit poses/delays below.
Hot-reloads itself when the file changes on disk."""

import subprocess, time, sys, os

# ── Edit these ──────────────────────────────────────────────────
POSES = [
    # [j1, j2, j3, j4, j5, j6],  delay_after_secs
    ([0.0,  0.0,  0.0,  0.0, 0.0, 0.0],   0.0),
    ([0.0, -0.5,  0.5,  0.3, 0.0, 0.0],   0.0),
    ([0.5, -0.3,  0.8, -0.2, 0.0, 0.0],   0.0),
    ([0.0,  0.0,  0.0,  0.0, 0.0, 0.0],   0.0),

    ([0.0,  0.0,  0.5,  0.0, 0.0, 0.0],   0.0),
    ([0.0,  0.0,  0.2,  0.0, 0.0, 0.0],   0.0),
    ([0.0,  0.0,  -1.5, 0.0, 0.0, 0.0],   0.0),

     ([0.0,  -0.3,  -0.2,  0.0, 0.0, 0.0],   0.0),
     ([0.0,  .8,  -0.5,  0.0, 0.0, 0.0],   0.0),
     ([0.0,  0.0,  -0.2, 0.0, 0.0, 0.0],   0.0),

     ([0.0,   0.0,   0.0,   0.0,  0.0,  0.0],   0.0),
     ([0.8,   0.3,  -0.3,   0.5,  1.0,  0],   0.0),
     ([-0.8,  0.2,  -0.5,  -0.8,  -1.0, 0],   0.0),
     ([1.0,   0.4,  -0.2,   1.2,  1.5,  0],   0.0),
     ([-1.0,  0.1,  -0.4,  -1.2,  -1.5, 0],   0.0),
     ([0.0,   0.0,   0.0,   0.0,  0.0,  0.0],   0.0), 

     ([0.0,   -0.8,   0.0,   0.0,  0.0,  0.0],   0.0),
     ([0.8,   -0.8,  -0.3,   0.5,  1.0,  0],   0.0),
     ([-0.8,  -0.8,  -0.5,  -0.8,  -1.0, 0],   0.0),
     ([1.0,   -0.8,  -0.2,   1.2,  1.5,  0],   0.0),
     ([-1.0,  -0.8,  -0.4,  -1.2,  -1.5, 0],   0.0),
     ([0.0,   -0.8,   0.0,   0.0,  0.0,  0.0],   0.0), 

    #  Far base swing: arm extended out, big J1 sweep
     ([1.4,   0.7,  -0.9,   0.0,  0.0,  0.0],   0.0),
     ([-1.4,  0.7,  -0.9,   0.0,  0.0,  0.0],   0.0),
     ([.4,   0.7,  -0.9,   0.0,  0.0,  0.0],   0.0),
     ([-.4,  0.7,  -0.9,   0.0,  0.0,  0.0],   0.0),

     # Folded-up near base swing: arm tucked tight, big J1 sweep
     ([1.4,  -0.3,  1.5,   -1.0,  0.0,  0.0],   0.0),
     ([-1.4, -0.3,  1.5,   -1.0,  0.0,  0.0],   0.0),
     ([.4,  -0.3,  1.5,   -1.0,  0.0,  0.0],   0.0),
     ([-.4, -0.3,  1.5,   -1.0,  0.0,  0.0],   0.0),

]

MOVE_TIME = 0.1   # seconds for each move
LOOPS = 0         # 0 = infinite
# ────────────────────────────────────────────────────────────────

_SELF = os.path.abspath(__file__)
_MTIME = os.path.getmtime(_SELF)

def _check_reload():
    """Re-exec this script if the file has been modified and parses cleanly."""
    if os.path.getmtime(_SELF) != _MTIME:
        # Syntax-check before reloading
        r = subprocess.run([sys.executable, "-m", "py_compile", _SELF],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"\n⚠️  File changed but has syntax errors — skipping reload:\n{r.stderr.strip()}")
            return
        print("\n🔄 File changed — hot-reloading...\n")
        os.execv(sys.executable, [sys.executable] + sys.argv)

def goto_js(joints, t):
    data_str = ",".join(f"{v}" for v in joints)
    msg = f"{{data: {{data: [{data_str}]}}, time: {t}}}"
    cmd = ["ros2", "service", "call", "/mars/arm/goto_js_v2", "maurice_msgs/srv/GotoJS", msg]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    ok = "success=True" in r.stdout
    return ok

def main():
    loop = 0
    try:
        while LOOPS == 0 or loop < LOOPS:
            loop += 1
            print(f"\n═══ Loop {loop} ═══")
            for i, (joints, delay) in enumerate(POSES):
                _check_reload()
                label = " ".join(f"{v:+.2f}" for v in joints)
                print(f"  [{i+1}/{len(POSES)}] goto [{label}]  t={MOVE_TIME}s ... ", end="", flush=True)
                ok = goto_js(joints, MOVE_TIME)
                print("✓" if ok else "✗")
                time.sleep(.5)
                if delay > 0:
                    time.sleep(delay)
    except KeyboardInterrupt:
        print("\nStopped.")

if __name__ == "__main__":
    main()
