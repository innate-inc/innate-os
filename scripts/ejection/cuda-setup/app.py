#!/usr/bin/env python3
"""
CUDA Setup Web Interface
A simple web UI to install CUDA dependencies on the robot via SSH
"""

import subprocess
import os
from flask import Flask, render_template, jsonify
from pathlib import Path

app = Flask(__name__)

# Configuration
ROBOT_HOST = os.environ.get("ROBOT_HOST", "jetson1@192.168.55.1")
ROBOT_IP = ROBOT_HOST.split("@")[-1]
ROBOT_PASSWORD = os.environ.get("ROBOT_PASSWORD", "goodbot")
KNOWN_HOSTS = Path.home() / ".ssh" / "known_hosts"


def run_command(cmd, timeout=300):
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


def ssh_cmd(remote_cmd, timeout=300, use_sudo=False):
    """Run a command on the robot via SSH"""
    if use_sudo:
        remote_cmd = f"echo '{ROBOT_PASSWORD}' | sudo -S {remote_cmd}"
    cmd = f'''sshpass -p "{ROBOT_PASSWORD}" ssh -o StrictHostKeyChecking=no {ROBOT_HOST} "{remote_cmd}" 2>&1'''
    return run_command(cmd, timeout=timeout)


@app.route("/")
def index():
    return render_template("index.html", robot_host=ROBOT_HOST, robot_ip=ROBOT_IP)


@app.route("/api/clear-known-hosts", methods=["POST"])
def clear_known_hosts():
    """Remove robot IP from known_hosts"""
    cmd = f'ssh-keygen -f "{KNOWN_HOSTS}" -R "{ROBOT_IP}" 2>&1 || true'
    return jsonify(run_command(cmd))


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


@app.route("/api/install-nvrtc", methods=["POST"])
def install_nvrtc():
    """Install cuda-nvrtc-dev-12-6"""
    result = ssh_cmd("apt-get install -y cuda-nvrtc-dev-12-6", timeout=300, use_sudo=True)
    if result["success"]:
        result["output"] = "✓ cuda-nvrtc-dev-12-6 installed\n" + result["output"]
    return jsonify(result)


@app.route("/api/create-symlink", methods=["POST"])
def create_symlink():
    """Create CUDA symlink"""
    # First check if symlink already exists
    check_result = ssh_cmd("ls -la /usr/local/cuda 2>&1 || true", timeout=30)
    
    if "/usr/local/cuda-12.6" in check_result.get("output", ""):
        return jsonify({
            "success": True,
            "output": "✓ Symlink already exists: /usr/local/cuda -> /usr/local/cuda-12.6",
            "returncode": 0
        })
    
    # Remove existing symlink if it points elsewhere
    ssh_cmd("rm -f /usr/local/cuda", timeout=30, use_sudo=True)
    
    # Create new symlink
    result = ssh_cmd("ln -s /usr/local/cuda-12.6 /usr/local/cuda", timeout=30, use_sudo=True)
    if result["success"]:
        result["output"] = "✓ Created symlink: /usr/local/cuda -> /usr/local/cuda-12.6\n" + result["output"]
    return jsonify(result)


@app.route("/api/install-cudart", methods=["POST"])
def install_cudart():
    """Install cuda-cudart-dev-12-6"""
    result = ssh_cmd("apt-get install -y cuda-cudart-dev-12-6", timeout=300, use_sudo=True)
    if result["success"]:
        result["output"] = "✓ cuda-cudart-dev-12-6 installed\n" + result["output"]
    return jsonify(result)


@app.route("/api/install-all", methods=["POST"])
def install_all():
    """Run all CUDA installation steps in order"""
    outputs = []
    all_success = True
    
    # Step 1: Install nvrtc
    outputs.append("=== Step 1/3: Installing cuda-nvrtc-dev-12-6 ===")
    result = ssh_cmd("apt-get install -y cuda-nvrtc-dev-12-6", timeout=300, use_sudo=True)
    outputs.append(result["output"])
    if not result["success"]:
        all_success = False
        outputs.append("❌ Step 1 failed")
    else:
        outputs.append("✓ Step 1 complete")
    
    # Step 2: Create symlink
    outputs.append("\n=== Step 2/3: Creating CUDA symlink ===")
    ssh_cmd("rm -f /usr/local/cuda", timeout=30, use_sudo=True)
    result = ssh_cmd("ln -s /usr/local/cuda-12.6 /usr/local/cuda", timeout=30, use_sudo=True)
    outputs.append(result["output"])
    if not result["success"]:
        all_success = False
        outputs.append("❌ Step 2 failed")
    else:
        outputs.append("✓ Step 2 complete: /usr/local/cuda -> /usr/local/cuda-12.6")
    
    # Step 3: Install cudart
    outputs.append("\n=== Step 3/3: Installing cuda-cudart-dev-12-6 ===")
    result = ssh_cmd("apt-get install -y cuda-cudart-dev-12-6", timeout=300, use_sudo=True)
    outputs.append(result["output"])
    if not result["success"]:
        all_success = False
        outputs.append("❌ Step 3 failed")
    else:
        outputs.append("✓ Step 3 complete")
    
    outputs.append("\n" + ("✓ All steps completed successfully!" if all_success else "⚠️ Some steps failed"))
    
    return jsonify({
        "success": all_success,
        "output": "\n".join(outputs),
        "returncode": 0 if all_success else 1
    })


@app.route("/api/check-cuda", methods=["POST"])
def check_cuda():
    """Check current CUDA installation status"""
    outputs = []
    
    # Check symlink
    result = ssh_cmd("ls -la /usr/local/cuda 2>&1 || echo 'No symlink found'", timeout=30)
    outputs.append("=== CUDA Symlink ===")
    outputs.append(result["output"])
    
    # Check installed packages
    result = ssh_cmd("dpkg -l | grep -E 'cuda-nvrtc-dev|cuda-cudart-dev' 2>&1 || echo 'No CUDA dev packages found'", timeout=30)
    outputs.append("\n=== Installed CUDA Packages ===")
    outputs.append(result["output"])
    
    # Check nvcc
    result = ssh_cmd("/usr/local/cuda/bin/nvcc --version 2>&1 || echo 'nvcc not found'", timeout=30)
    outputs.append("\n=== NVCC Version ===")
    outputs.append(result["output"])
    
    return jsonify({
        "success": True,
        "output": "\n".join(outputs),
        "returncode": 0
    })


if __name__ == "__main__":
    print(f"""
╔═══════════════════════════════════════════════════════════════╗
║              CUDA Setup Web Interface                         ║
╠═══════════════════════════════════════════════════════════════╣
║  Robot: {ROBOT_HOST:<52} ║
║  URL:   http://localhost:5051                                 ║
╚═══════════════════════════════════════════════════════════════╝
""")
    app.run(host="0.0.0.0", port=5051, debug=True)

