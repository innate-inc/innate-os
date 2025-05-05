# GR00T Installation on Jetson for Remote Policy

This document summarizes the steps taken to install the `gr00t` package and its dependencies (specifically `decord`) on an NVIDIA Jetson device, intended for use with the `remote_policy.py` script in this directory.

## Initial GR00T Setup

These steps were performed within the cloned `Isaac-GR00T` repository (`cd /path/to/Isaac-GR00T`).

1.  **Editable Install Attempt:**
    *   Tried `uv pip install -e . --system`. Encountered permission errors.
    *   Tried `sudo pip install -e .`. Encountered build backend errors.

2.  **Fix Build Backend:**
    *   Edited `Isaac-GR00T/pyproject.toml`:
        *   Changed `build-backend = "setuptools.build_meta:__legacy__"`
        *   To `build-backend = "setuptools.build_meta"`

3.  **Dependency Installation Issue (`antlr4-python3-runtime`):
    *   Ran `sudo pip install -e .` again.
    *   Encountered `TypeError: canonicalize_version() got an unexpected keyword argument 'strip_trailing_zero'` during `antlr4-python3-runtime` metadata generation.
    *   Tried updating `pip` and `setuptools`: `sudo python3 -m pip install --upgrade pip setuptools` (they were already up-to-date).
    *   Tried installing `antlr4-python3-runtime` separately: `sudo pip install 'antlr4-python3-runtime>=4.9,<4.10'` (failed with the same error).

4.  **Downgrade Setuptools:**
    *   Downgraded `setuptools` globally: `sudo pip install setuptools==59.6.0`

5.  **Successful Editable Install:**
    *   Ran `sudo pip install -e .` again (within `Isaac-GR00T` directory).
    *   Installation completed successfully.

## Installing `decord` Dependency

This is required by `gr00t` but needs manual building on Jetson.

1.  **Installation Attempt:**
    *   Ran `sudo pip install decord`.
    *   Failed with `Could not find a version that satisfies the requirement decord`. No pre-built wheels available for Jetson (aarch64).

2.  **Build from Source:**
    *   **Prerequisites:** Install build tools and FFmpeg libraries (commands may vary based on Jetpack version):
        ```bash
        # Example for Ubuntu 20.04 based Jetpack
        # sudo add-apt-repository ppa:jonathonf/ffmpeg-4 # May not be needed on newer Jetpack
        sudo apt-get update
        sudo apt-get install -y build-essential python3-dev python3-setuptools make cmake
        sudo apt-get install -y ffmpeg libavcodec-dev libavfilter-dev libavformat-dev libavutil-dev libavresample-dev # Added libavresample-dev potentially needed
        ```
    *   **Clone Repository:**
        ```bash
        git clone --recursive https://github.com/dmlc/decord ~/decord # Clone to home dir or other location
        cd ~/decord
        ```
    *   **Configure Build (CPU Only Recommended for Jetson):**
        ```bash
        mkdir build && cd build
        cmake .. -DUSE_CUDA=OFF -DCMAKE_BUILD_TYPE=Release 
        # Note: Using USE_CUDA=ON often causes issues on Jetson due to missing libnvcuvid.so
        ```
    *   **Compile:**
        ```bash
        make -j$(nproc) # Use multiple cores for faster build
        ```
    *   **Install Python Package:**
        ```bash
        cd ../python
        sudo python3 setup.py install --user # Installs to user site-packages
        ```

3.  **Runtime Library Not Found Fix:**
    *   Importing `decord` in Python failed with `RuntimeError: Cannot find the files. [/path/to/libdecord.so]`. The `setup.py install` did not correctly place the shared library.
    *   Located the built library at `~/decord/build/libdecord.so`.
    *   Manually copied the library to the location expected by the Python package:
        ```bash
        # Adjust username (jetson1) and python version (3.10) if necessary
        mkdir -p /home/jetson1/.local/lib/python3.10/site-packages/decord/
        cp ~/decord/build/libdecord.so /home/jetson1/.local/lib/python3.10/site-packages/decord/
        ```

## Current System Status

*   `gr00t` package installed globally in editable mode (`-e`).
*   `decord` package built from source (CPU-only) and installed globally for the user, with manual library placement.
*   System `setuptools` is currently downgraded to `59.6.0`.

*(This README documents the specific steps taken for this setup)* 