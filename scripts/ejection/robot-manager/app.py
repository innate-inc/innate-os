#!/usr/bin/env python3
"""
Robot Manager Web Interface
A simple web UI to manage robot operations via SSH
"""

import subprocess
import os
from flask import Flask, render_template, jsonify, request
from pathlib import Path

app = Flask(__name__)

# Configuration
ROBOT_HOST = os.environ.get("ROBOT_HOST", "jetson1@192.168.55.1")
ROBOT_IP = ROBOT_HOST.split("@")[-1]
ROBOT_PASSWORD = os.environ.get("ROBOT_PASSWORD", "goodbot")
SCRIPT_DIR = Path(__file__).parent.parent
KNOWN_HOSTS = Path.home() / ".ssh" / "known_hosts"


def run_command(cmd, timeout=120):
    """Run a shell command and return output"""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "ROBOT_PASSWORD": ROBOT_PASSWORD}
        )
        return {
            "success": result.returncode == 0,
            "output": result.stdout + result.stderr,
            "returncode": result.returncode
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "output": "Command timed out", "returncode": -1}
    except Exception as e:
        return {"success": False, "output": str(e), "returncode": -1}


@app.route("/")
def index():
    return render_template("index.html", robot_host=ROBOT_HOST, robot_ip=ROBOT_IP)


@app.route("/api/clear-known-hosts", methods=["POST"])
def clear_known_hosts():
    """Remove robot IP from known_hosts"""
    cmd = f'ssh-keygen -f "{KNOWN_HOSTS}" -R "{ROBOT_IP}" 2>&1 || true'
    return jsonify(run_command(cmd))


@app.route("/api/copy-wave", methods=["POST"])
def copy_wave():
    """Copy wave primitive to robot"""
    script = SCRIPT_DIR / "copy-wave-primitive.sh"
    # Use sshpass to avoid password prompts, with StrictHostKeyChecking disabled
    cmd = f'sshpass -p "{ROBOT_PASSWORD}" ssh -o StrictHostKeyChecking=no {ROBOT_HOST} "mkdir -p /home/jetson1/innate-os/primitives/wave" 2>&1'
    result1 = run_command(cmd)
    
    # Find h5 file
    wave_dir = SCRIPT_DIR.parent.parent / "primitives" / "wave"
    h5_files = list(wave_dir.glob("*.h5"))
    
    if not h5_files:
        return jsonify({"success": False, "output": "No .h5 files found in primitives/wave", "returncode": 1})
    
    # Copy h5 file
    h5_file = h5_files[0]
    cmd = f'sshpass -p "{ROBOT_PASSWORD}" scp -o StrictHostKeyChecking=no "{h5_file}" {ROBOT_HOST}:/home/jetson1/innate-os/primitives/wave/ 2>&1'
    result = run_command(cmd, timeout=300)  # 5 min timeout for large file
    
    if result["success"]:
        result["output"] = f"✓ Copied {h5_file.name} ({h5_file.stat().st_size // (1024*1024)}MB) to robot\n" + result["output"]
    
    return jsonify(result)


@app.route("/api/clear-maps", methods=["POST"])
def clear_maps():
    """Clear maps from robot"""
    cmd = f'''sshpass -p "{ROBOT_PASSWORD}" ssh -o StrictHostKeyChecking=no {ROBOT_HOST} "rm -f /home/jetson1/innate-os/maps/*.pgm /home/jetson1/innate-os/maps/*.yaml && echo '✓ Maps cleared'" 2>&1'''
    return jsonify(run_command(cmd))


@app.route("/api/speaker-test", methods=["POST"])
def speaker_test():
    """Run speaker test on robot"""
    duration = request.json.get("duration", 10) if request.json else 10
    # Use gst-launch with volume=0.3 (30%) for quieter test tone
    cmd = f'''sshpass -p "{ROBOT_PASSWORD}" ssh -o StrictHostKeyChecking=no {ROBOT_HOST} "timeout {duration} gst-launch-1.0 audiotestsrc freq=440 volume=0.3 ! audioconvert ! alsasink 2>&1 && echo 'Speaker test complete' || true" 2>&1'''
    result = run_command(cmd, timeout=duration + 10)
    if "Setting pipeline to PLAYING" in result["output"] or "Speaker test complete" in result["output"]:
        result["success"] = True
        result["output"] = f"✓ Speaker test ran for {duration}s\n" + result["output"]
    return jsonify(result)


@app.route("/api/shutdown", methods=["POST"])
def shutdown():
    """Shutdown the robot"""
    cmd = f'''sshpass -p "{ROBOT_PASSWORD}" ssh -o StrictHostKeyChecking=no {ROBOT_HOST} "echo '{ROBOT_PASSWORD}' | sudo -S shutdown now" 2>&1 || true'''
    result = run_command(cmd)
    result["output"] = "✓ Shutdown command sent\n" + result["output"]
    result["success"] = True
    return jsonify(result)


@app.route("/api/ping", methods=["POST"])
def ping():
    """Check if robot is reachable"""
    cmd = f'ping -c 1 -W 2 {ROBOT_IP} 2>&1'
    result = run_command(cmd, timeout=5)
    result["success"] = "1 packets received" in result["output"] or "1 received" in result["output"]
    return jsonify(result)


@app.route("/api/ssh-check", methods=["POST"])
def ssh_check():
    """Check SSH connection"""
    cmd = f'''sshpass -p "{ROBOT_PASSWORD}" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 {ROBOT_HOST} "echo '✓ SSH connection successful' && hostname" 2>&1'''
    return jsonify(run_command(cmd, timeout=10))


@app.route("/api/run-command", methods=["POST"])
def run_custom_command():
    """Run a custom command on the robot"""
    data = request.json
    if not data or "command" not in data:
        return jsonify({"success": False, "output": "No command provided", "returncode": 1})
    
    remote_cmd = data["command"]
    cmd = f'''sshpass -p "{ROBOT_PASSWORD}" ssh -o StrictHostKeyChecking=no {ROBOT_HOST} "{remote_cmd}" 2>&1'''
    return jsonify(run_command(cmd, timeout=30))


if __name__ == "__main__":
    print(f"""
╔═══════════════════════════════════════════════════════════════╗
║           Robot Manager Web Interface                         ║
╠═══════════════════════════════════════════════════════════════╣
║  Robot: {ROBOT_HOST:<52} ║
║  URL:   http://localhost:5050                                 ║
╚═══════════════════════════════════════════════════════════════╝
""")
    app.run(host="0.0.0.0", port=5050, debug=True)

