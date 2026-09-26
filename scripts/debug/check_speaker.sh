#!/usr/bin/env bash
# Speaker troubleshooter for MARS. Walks the audio chain from the cheap causes
# (volume at zero) down to the hardware (I2S route, amp wiring), then plays a
# test tone and the robot's own voice and asks what was heard. Read-only except
# a temporary volume boost, restored on exit, and one test line in the chat.
#
#   scripts/debug/check_speaker.sh            # full check with audible tests
#   scripts/debug/check_speaker.sh --no-sound # skip the tone and voice tests (remote / silent)
#
# Chain: app volume -> softvol "Master" -> dmix -> hw:APE,0 (ADMAIF1)
#        -> XBAR "I2S2 Mux" -> I2S pins -> MAX98357A amp -> speaker

set -u

ROOT="${INNATE_OS_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
CARD=APE
PCM_STATUS=/proc/asound/$CARD/pcm0p/sub0/status
AUDIBLE_FLOOR=55 # mirrors apply_alsa_volume() in mars_control/app.cpp
PLAY_SOUND=1
[[ "${1:-}" == "--no-sound" ]] && PLAY_SOUND=0
[[ -t 0 ]] || PLAY_SOUND=0

if [[ -t 1 ]]; then
    R=$'\e[31m' G=$'\e[32m' Y=$'\e[33m' B=$'\e[1m' D=$'\e[2m' N=$'\e[0m'
else
    R='' G='' Y='' B='' D='' N=''
fi

FAILS=() WARNS=()
ok() { echo "  ${G}[ OK ]${N} $*"; }
warn() { echo "  ${Y}[WARN]${N} $*"; WARNS+=("$*"); }
fail() { echo "  ${R}[FAIL]${N} $*"; FAILS+=("$*"); }
info() { echo "  ${D}[INFO]${N} $*"; }
fix() { echo "         ${D}fix:${N} $*"; }
section() { echo; echo "${B}== $* ==${N}"; }
have() { command -v "$1" >/dev/null 2>&1; }

ask() { # ask "question" -> 0 for yes
    local reply
    read -r -p "  >> $1 [y/n] " reply
    [[ "$reply" =~ ^[Yy] ]]
}

# The same sample on both slots: the mono amp plays one slot or their average, per its SD pin.
tone() { # tone SECONDS HZ
    python3 - "$@" <<'EOF' | timeout $(( ${1%.*} + 5 )) aplay -q -D default -t raw -f S16_LE -r 48000 -c 2
import math, struct, sys
secs, hz = float(sys.argv[1]), float(sys.argv[2])
amp = 8000  # ~-12 dBFS: softvol's +12 dB at 100% must not clip it
out = bytearray()
for i in range(int(48000 * secs)):
    s = int(amp * math.sin(2 * math.pi * hz * i / 48000))
    out += struct.pack("<hh", s, s)
sys.stdout.buffer.write(out)
EOF
}

ros() { # ros "ros2 ..." -> runs it in a ROS-sourced shell
    bash -c "source /opt/ros/humble/setup.bash && source '$ROOT/ros2_ws/install/setup.bash' \
        && export RMW_IMPLEMENTATION=\${RMW_IMPLEMENTATION:-rmw_zenoh_cpp} && $1" 2>/dev/null
}

master_pct() { amixer -M -c "$CARD" sget Master 2>/dev/null | grep -o '\[[0-9]*%\]' | head -1 | tr -d '[]%'; }
master_raw() { amixer -c "$CARD" sget Master 2>/dev/null | grep -oE 'Left: [0-9]+' | head -1 | grep -oE '[0-9]+'; }

SAVED_RAW=""
restore_volume() { [[ -n "$SAVED_RAW" ]] && amixer -q -c "$CARD" sset Master "$SAVED_RAW" 2>/dev/null; }
trap restore_volume EXIT INT TERM

echo "${B}MARS speaker check${N}  $(hostname)  $(date '+%F %T')"
[[ $EUID -eq 0 ]] && warn "running as root: run as the robot user so the check sees what the robot sees"

# ---------------------------------------------------------------------------
section "1. Volume"

VOLUME_PCT=""
ROBOT_INFO="$ROOT/data/robot_info.json"
if [[ -f "$ROBOT_INFO" ]]; then
    VOLUME_PCT=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('volume_percent', 80))" "$ROBOT_INFO" 2>/dev/null)
    if [[ -z "$VOLUME_PCT" ]]; then
        fail "robot_info.json is not valid JSON: $ROBOT_INFO"
    elif (( VOLUME_PCT == 0 )); then
        fail "robot volume is set to 0% in the app (muted)"
        fix "raise it in the app, or: innate volume 60"
    elif (( VOLUME_PCT < 20 )); then
        warn "robot volume is low: ${VOLUME_PCT}% in the app"
        fix "innate volume 60"
    else
        ok "robot volume setting: ${VOLUME_PCT}%"
    fi
else
    info "no robot_info.json yet (app never started?): volume defaults to 80%"
fi

LIVE_PCT=$(master_pct)
if [[ -z "$LIVE_PCT" ]]; then
    fail "ALSA 'Master' volume control does not exist on card $CARD"
    info "it is created by the softvol in /etc/asound.conf the first time anything plays; see section 2"
elif (( LIVE_PCT == 0 )); then
    fail "ALSA 'Master' volume is 0%"
    fix "innate volume 60   (or: amixer -M -c $CARD sset Master 80%)"
else
    ok "ALSA 'Master' volume: ${LIVE_PCT}%"
    if [[ -n "$VOLUME_PCT" ]] && (( VOLUME_PCT > 0 && VOLUME_PCT < 100 )); then
        expected=$(( AUDIBLE_FLOOR + (100 - AUDIBLE_FLOOR) * VOLUME_PCT / 100 ))
        if (( LIVE_PCT < expected - 3 || LIVE_PCT > expected + 3 )); then
            warn "'Master' is ${LIVE_PCT}% but the app setting ${VOLUME_PCT}% maps to ~${expected}%: the app never applied it"
            fix "innate volume ${VOLUME_PCT}   (re-applies it through /set_volume)"
        fi
    fi
fi

# ---------------------------------------------------------------------------
section "2. ALSA configuration"

for tool in aplay amixer; do
    have "$tool" || { fail "$tool is not installed"; fix "sudo apt install alsa-utils"; }
done

if [[ ! -f /etc/asound.conf ]]; then
    fail "/etc/asound.conf is missing: the default device is not the speaker and there is no 'Master' volume"
    fix "sudo cp $ROOT/config/alsa/asound.conf /etc/asound.conf"
elif [[ -f "$ROOT/config/alsa/asound.conf" ]] && ! cmp -s /etc/asound.conf "$ROOT/config/alsa/asound.conf"; then
    warn "/etc/asound.conf differs from the shipped config/alsa/asound.conf"
    fix "sudo cp $ROOT/config/alsa/asound.conf /etc/asound.conf"
else
    ok "/etc/asound.conf matches the shipped dmix + softvol config"
fi

for rc in "$HOME/.asoundrc" /root/.asoundrc; do
    [[ -r "$rc" ]] || continue
    warn "$rc exists and can override the default device"
    fix "mv $rc $rc.bak"
done

APE_NUM=$(sed -n "s/^ *\([0-9]*\) \[$CARD *\].*/\1/p" /proc/asound/cards 2>/dev/null)
for server in pulseaudio pipewire; do
    for pid in $(pgrep -x "$server"); do
        ls -l "/proc/$pid/fd" 2>/dev/null | grep -q "/dev/snd/pcmC${APE_NUM:-X}D" || continue
        warn "$server (pid $pid) holds the speaker device: it can block or reroute playback"
        fix "systemctl --user stop $server.socket $server.service (PulseAudio is purged by innate update)"
    done
done

# ---------------------------------------------------------------------------
section "3. Sound card and I2S (kernel side)"

if grep -q "\[$CARD *\]" /proc/asound/cards 2>/dev/null; then
    ok "Tegra APE sound card is present"
else
    fail "Tegra APE sound card is missing: the audio driver did not load"
    info "cards seen: $(grep -o '\[[^]]*\]' /proc/asound/cards 2>/dev/null | tr -d '[] ' | paste -sd, -)"
fi

EXTLINUX=/boot/extlinux/extlinux.conf
OVERLAY=$(grep -E '^\s*OVERLAYS' "$EXTLINUX" 2>/dev/null | grep -io '[^ ]*uda1334a[^ ]*' | head -1)
if [[ -z "$OVERLAY" ]]; then
    fail "the I2S overlay (Adafruit UDA1334A) is not in $EXTLINUX: the header pins are not I2S"
    fix "sudo /opt/nvidia/jetson-io/config-by-hardware.py -n 'Adafruit UDA1334A' && sudo reboot"
elif [[ ! -f "$OVERLAY" ]]; then
    fail "extlinux.conf names $OVERLAY but that file does not exist"
    fix "sudo /opt/nvidia/jetson-io/config-by-hardware.py -n 'Adafruit UDA1334A' && sudo reboot"
else
    ok "I2S overlay configured: $(basename "$OVERLAY")"
fi

ROUTE=$(amixer -c "$CARD" sget 'I2S2 Mux' 2>/dev/null | grep -o "Item0: '[^']*'" | cut -d"'" -f2)
if [[ "$ROUTE" == "ADMAIF1" ]]; then
    ok "audio crossbar routes playback to the amp (I2S2 Mux = ADMAIF1)"
else
    fail "audio crossbar is not routed to the amp: I2S2 Mux = '${ROUTE:-unreadable}', must be ADMAIF1 (plays fine, but silently)"
    fix "amixer -c $CARD cset name='I2S2 Mux' ADMAIF1 && sudo alsactl store"
fi
if ! grep -A1 "name 'I2S2 Mux'" /var/lib/alsa/asound.state 2>/dev/null | grep -q "value ADMAIF1"; then
    warn "the I2S2 route is not saved in /var/lib/alsa/asound.state: it will be lost on reboot"
    fix "sudo alsactl store   (while the route is correct)"
fi
systemctl is-active --quiet alsa-restore 2>/dev/null \
    || { warn "alsa-restore.service is not active: saved mixer routes are not applied at boot"; fix "sudo systemctl enable --now alsa-restore"; }

if id -nG | grep -qw audio; then
    ok "user $(id -un) is in the audio group"
else
    fail "user $(id -un) is not in the audio group: cannot open the sound device"
    fix "sudo usermod -aG audio $(id -un) && reboot"
fi

KERNEL_ERRS=$(journalctl -k -b --no-pager 2>/dev/null | grep -iE 'asoc|ahub|admaif|tegra.*i2s|snd' | grep -iE 'err|fail|timeout' | tail -5)
if [[ -n "$KERNEL_ERRS" ]]; then
    warn "kernel audio errors this boot:"
    sed 's/^/           /' <<<"$KERNEL_ERRS"
else
    ok "no kernel audio errors this boot"
fi

# ---------------------------------------------------------------------------
section "4. Playback path"

if systemctl is-active --quiet speaker-keepalive 2>/dev/null; then
    ok "speaker-keepalive.service is running"
else
    warn "speaker-keepalive.service is not running: expect pops and a clipped start of every sound"
    journalctl -u speaker-keepalive -n 3 --no-pager 2>/dev/null | tail -3 | sed 's/^/           /'
    fix "sudo systemctl restart speaker-keepalive"
fi

OUT=$(timeout 5 aplay -q -D default -f S16_LE -r 48000 -c 2 -d 1 /dev/zero 2>&1)
rc=$?
if (( rc == 0 )); then
    ok "the default output opens and accepts audio"
elif (( rc == 124 )); then
    fail "playback hangs: the device accepts audio but never consumes it (I2S clock stuck)"
    fix "sudo systemctl restart speaker-keepalive; if it persists, reboot"
else
    fail "cannot play to the default output: ${OUT:-rc=$rc}"
    if grep -qi busy <<<"$OUT"; then
        owner=$(grep -oE 'owner_pid *: *[0-9]+' "$PCM_STATUS" 2>/dev/null | grep -oE '[0-9]+$')
        [[ -n "$owner" ]] && info "hw:$CARD,0 is held by pid $owner: $(ps -o args= -p "$owner" 2>/dev/null)"
        fix "stop whatever opened hw:$CARD,0 directly (everything must go through 'default')"
    fi
fi

# The hardware pointer only moves while the audio engine consumes frames; the route to the pins is checked in section 3.
bg=""
grep -q RUNNING "$PCM_STATUS" 2>/dev/null || { aplay -q -D default -f S16_LE -r 48000 -c 2 -d 2 /dev/zero 2>/dev/null & bg=$!; sleep 0.5; }
p1=$(grep -oE 'hw_ptr *: *[0-9]+' "$PCM_STATUS" 2>/dev/null | grep -oE '[0-9]+$')
sleep 0.3
p2=$(grep -oE 'hw_ptr *: *[0-9]+' "$PCM_STATUS" 2>/dev/null | grep -oE '[0-9]+$')
[[ -n "$bg" ]] && wait "$bg" 2>/dev/null
if [[ -n "$p1" && -n "$p2" && "$p2" != "$p1" ]]; then
    ok "the audio engine is consuming audio (hardware pointer advancing)"
elif [[ -n "$p1" ]]; then
    fail "the hardware pointer is not moving: the audio engine is stuck and nothing reaches the amp"
    fix "reboot; if it persists, reflash the I2S overlay (section 3)"
else
    warn "could not read $PCM_STATUS to confirm the audio engine is running"
fi

STALE=$(ps -eo pid,etimes,args | awk '$3 ~ /aplay$/ && $0 !~ /\/dev\/zero/ && $2 > 120 {print}')
if [[ -n "$STALE" ]]; then
    warn "aplay processes stuck for over 2 min (a TTS clip that never finished):"
    sed 's/^/           /' <<<"$STALE"
    fix "kill the pids above"
fi

# ---------------------------------------------------------------------------
section "5. Robot voice (TTS) and sounds"

if systemctl is-active --quiet ros-app 2>/dev/null; then
    ok "ros-app.service is running"
else
    fail "ros-app.service is not running: the robot never speaks"
    fix "innate restart"
fi

if grep -qE '^\s*simulator_mode:\s*true' "$ROOT/config/settings.yaml" 2>/dev/null; then
    fail "simulator_mode is true in config/settings.yaml: speech goes to the webapp, not the speaker"
fi

if [[ -n "${INNATE_SERVICE_KEY:-}" ]] || grep -qsE '^\s*INNATE_SERVICE_KEY=.+' "$ROOT/.env" /etc/innate.env; then
    ok "Innate service key is set (TTS goes through the Innate proxy)"
else
    fail "no INNATE_SERVICE_KEY in $ROOT/.env or /etc/innate.env: TTS cannot authenticate (tones work, the robot never talks)"
fi

PROXY="${INNATE_PROXY_URL:-https://proxy-v1.svc.innate.bot}"
if have curl; then
    code=$(curl -s -o /dev/null -m 6 -w '%{http_code}' "$PROXY" 2>/dev/null)
    if [[ "$code" =~ ^[1-5][0-9][0-9]$ ]]; then
        ok "TTS proxy reachable ($PROXY)"
    else
        fail "TTS proxy unreachable ($PROXY): no internet, so the robot cannot speak (tones still work)"
    fi
fi

TTS_ERRS=$(find "$HOME/.ros/log" -maxdepth 1 -name 'python3_*.log' -mtime -1 2>/dev/null \
    | xargs -r grep -hE 'aplay failed|TTS generation failed|Failed to initialize Cartesia|TTS not available' 2>/dev/null | tail -3)
if [[ -n "$TTS_ERRS" ]]; then
    warn "recent TTS errors in the robot logs:"
    sed 's/^/           /' <<<"$TTS_ERRS"
else
    ok "no TTS errors in the last day of robot logs"
fi

if [[ ! -f "$ROOT/config/sounds/turnon.wav" ]]; then
    warn "boot chime config/sounds/turnon.wav is missing: a silent boot looks like a dead speaker"
    fix "innate update   (re-downloads the sounds)"
fi

# ---------------------------------------------------------------------------
section "6. Listening test"

HEARD=""
if (( PLAY_SOUND == 0 )); then
    info "skipped (--no-sound or not interactive)"
elif ! have python3; then
    warn "python3 missing: cannot generate test tones"
else
    echo "  Stand next to the robot. Playing a 2 s tone at the current volume..."
    tone 2 660
    if ask "Did you hear the tone?"; then
        HEARD=yes
    else
        SAVED_RAW=$(master_raw)
        amixer -q -c "$CARD" sset Master 100% 2>/dev/null
        echo "  Playing again at full volume..."
        tone 2 660
        if ask "Did you hear it this time?"; then
            HEARD=loud-only
            warn "the speaker works but is inaudible at the current volume"
            fix "innate volume 80"
        else
            HEARD=no
        fi
        restore_volume
        SAVED_RAW=""
    fi
    [[ "$HEARD" != "no" ]] && ok "speaker is audible"

    if [[ ! -f "$ROOT/ros2_ws/install/setup.bash" ]]; then
        warn "ROS workspace not built: cannot test the robot's voice"
    else
        echo "  Asking the robot to speak through its real voice (brain -> TTS -> speaker)..."
        PLAYING=$(mktemp)
        # is_playing flips to "true" only once synthesized audio is streaming into aplay.
        ros "timeout 25 ros2 topic echo --once /tts/is_playing std_msgs/msg/String" >"$PLAYING" &
        watcher=$!
        sleep 3
        ros "timeout 15 ros2 topic pub --once -w 1 /brain/tts std_msgs/msg/String '{data: This is a speaker test.}'" >/dev/null
        wait "$watcher"
        if ! grep -q "true" "$PLAYING"; then
            fail "the robot never started speaking: its voice (TTS) is broken, not the speaker"
            info "see section 5 (service key, network), then the brain log: grep -i tts ~/.ros/log/python3_*.log | tail"
        elif [[ "$HEARD" == "no" ]]; then
            info "the robot's voice played but, like the tone, was not heard"
        elif ask "Did the robot say 'This is a speaker test'?"; then
            ok "the robot's voice works end to end"
        else
            fail "speech played but was not heard although the tone was: the TTS audio is silent or garbled"
            info "brain log: grep -i -e tts -e aplay ~/.ros/log/python3_*.log | tail"
        fi
        rm -f "$PLAYING"
    fi
fi

# ---------------------------------------------------------------------------
section "Summary"

if (( ${#FAILS[@]} )); then
    echo "  ${R}${B}${#FAILS[@]} problem(s) found.${N} Most likely cause:"
    echo "    ${B}${FAILS[0]}${N}"
    (( ${#FAILS[@]} > 1 )) && printf '    also: %s\n' "${FAILS[@]:1}"
elif [[ "$HEARD" == "no" ]]; then
    echo "  ${R}${B}Software is healthy and audio is routed to the I2S pins, but nothing is heard: this is hardware.${N}"
    cat <<EOF
    Power the robot off, then check, in order:
      1. Speaker leads are firmly on the amp's + / - screw terminals (not shorted together)
      2. Amp VIN has 5 V and GND is connected
      3. Amp SD pin is not pulled to GND (that shuts the amp down)
      4. I2S wires on the Jetson 40-pin header: BCLK pin 12, LRCLK pin 35, DIN pin 40
      5. Swap in a known-good speaker, then a known-good amp board
EOF
elif (( ${#WARNS[@]} )); then
    echo "  ${Y}${B}No blocking problem, ${#WARNS[@]} warning(s):${N}"
    printf '    - %s\n' "${WARNS[@]}"
else
    echo "  ${G}${B}Everything checks out.${N}"
fi
echo
