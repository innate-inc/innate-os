"""Build the optional patched XPBD module into sim/.xpbd (never the OS image).

Run: uv run --extra xpbd python sandbox/build_xpbd.py
Requires git, CMake, a C++17 compiler and OpenMP. macOS uses Homebrew LLVM.
The checkout, native dependencies and build outputs are local ignored files.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIN = "10a70bc146a97873dc3c8fef372f5217e010542e"
EIGEN_PIN = "3147391d946bb4b6c68edd901f2add6ac1f31f8c"


def run(*args, cwd=None):
    subprocess.run([str(a) for a in args], cwd=cwd, check=True)


def checkout(url, path, revision):
    if not path.exists():
        run("git", "clone", "--no-checkout", url, path)
        run("git", "-C", path, "checkout", "--detach", revision)
    actual = subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()
    if actual != revision:
        raise RuntimeError(f"Refusing to overwrite a different native checkout at {path}")


def main():
    if sys.version_info < (3, 11):
        raise RuntimeError("Use Python >=3.11, matching the world-server interpreter")
    cache = ROOT / ".xpbd"
    cache.mkdir(exist_ok=True)
    source, eigen = cache / "source", cache / "eigen"
    checkout("https://github.com/InteractiveComputerGraphics/PositionBasedDynamics.git", source, PIN)
    checkout("https://gitlab.com/libeigen/eigen.git", eigen, EIGEN_PIN)
    for name in ("xpbd_build.patch", "xpbd_batch_state.patch"):
        patch = ROOT / "sandbox" / name
        already = (
            subprocess.run(
                ["git", "apply", "--reverse", "--check", str(patch)],
                cwd=source,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        )
        if not already:
            run("git", "apply", "--check", patch, cwd=source)
            run("git", "apply", patch, cwd=source)
    compiler = []
    if sys.platform == "darwin":
        prefix = subprocess.check_output(["brew", "--prefix", "llvm"], text=True).strip()
        brew_prefix = subprocess.check_output(["brew", "--prefix"], text=True).strip()
        compiler = [
            f"-DCMAKE_CXX_COMPILER={prefix}/bin/clang++",
            f"-DCMAKE_C_COMPILER={prefix}/bin/clang",
            f"-DCMAKE_PREFIX_PATH={brew_prefix}",
        ]
    run(
        "cmake",
        "-S",
        source,
        "-B",
        cache / "build",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DPBD_LIBS_ONLY=ON",
        "-DUSE_PYTHON_BINDINGS=ON",
        "-DUSE_OpenMP=ON",
        f"-DEIGEN3_INCLUDE_DIR={eigen}",
        f"-DPython_EXECUTABLE={sys.executable}",
        f"-DPYTHON_EXECUTABLE={sys.executable}",
        *compiler,
    )
    run(
        "cmake",
        "--build",
        cache / "build",
        "--target",
        "pypbd",
        "-j",
        os.environ.get("CMAKE_BUILD_PARALLEL_LEVEL", "2"),
    )
    library = cache / "lib"
    library.mkdir(exist_ok=True)
    modules = list((cache / "build" / "lib").glob("pypbd*.so"))
    if len(modules) != 1:
        raise RuntimeError(f"Expected exactly one native module, got {modules}")
    shutil.copy2(modules[0], library / modules[0].name)
    shutil.copy2(source / "LICENSE", library / "LICENSE-PositionBasedDynamics")
    print(f"XPBD native module ready: {library}")


if __name__ == "__main__":
    main()
