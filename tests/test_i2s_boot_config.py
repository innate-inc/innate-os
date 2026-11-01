# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Exercise the hardware script's I2S section with temporary boot files.

Only the audio section runs: the root check and unrelated system configuration
are excluded, and both absolute audio paths point into pytest's temp directory.
No Jetson hardware, root privileges, or changes to the host are needed.
"""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/update/configure_hardware.sh"
PRIMARY = """LABEL primary
    MENU LABEL primary kernel
    LINUX /boot/Image
    INITRD /boot/initrd
    APPEND ${cbootargs} root=/dev/nvme0n1p1 rw
"""
JETSON_IO = """LABEL JetsonIO
    MENU LABEL Custom Header Config: <Adafruit UDA1334A>
    LINUX /boot/Image
    FDT /boot/kernel_tegra234-p3767-user-custom.dtb
    INITRD /boot/initrd
    APPEND ${cbootargs} root=/dev/nvme0n1p1 rw
"""
CONFIGURED = "DEFAULT JetsonIO\n" + PRIMARY + JETSON_IO
RESET = "DEFAULT primary\n" + PRIMARY + JETSON_IO


@pytest.fixture
def audio_setup(tmp_path):
    config = tmp_path / "extlinux.conf"
    jetson_io = tmp_path / "config-by-hardware.py"
    calls = tmp_path / "calls"
    repaired = tmp_path / "repaired.conf"
    repaired.write_text(CONFIGURED)
    jetson_io.write_text(
        "#!/bin/bash\n"
        'printf "%s\\n" "$@" >> "$TEST_CALLS"\n'
        'case "$TEST_REPAIR" in\n'
        '  repair) cp "$TEST_REPAIRED" "$TEST_CONFIG" ;;\n'
        "  noop) exit 0 ;;\n"
        "  fail) exit 1 ;;\n"
        "esac\n"
    )
    jetson_io.chmod(0o755)

    script = SCRIPT.read_text()
    start = script.index('log "Checking I2S audio configuration..."')
    end = script.index("# 1b. ALSA dmix configuration")
    section = script[start:end]
    section = section.replace("/boot/extlinux/extlinux.conf", str(config))
    section = section.replace("/opt/nvidia/jetson-io/config-by-hardware.py", str(jetson_io))

    def run(contents, *, repair="repair", jetson=True):
        if contents is not None:
            config.write_text(contents)
        if not jetson:
            jetson_io.unlink()
        result = subprocess.run(
            [
                "bash",
                "-c",
                'set -e\nlog() { echo "$*"; }\nREBOOT_REQUIRED=false\n'
                + section
                + '\nprintf "reboot=%s\\n" "$REBOOT_REQUIRED"\n',
            ],
            env={
                **os.environ,
                "TEST_CALLS": str(calls),
                "TEST_CONFIG": str(config),
                "TEST_REPAIRED": str(repaired),
                "TEST_REPAIR": repair,
            },
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout, calls.read_text().splitlines() if calls.exists() else [], config

    return run


@pytest.mark.parametrize(
    "contents",
    [
        CONFIGURED,
        "DEFAULT JetsonIO\n" + JETSON_IO + PRIMARY,
        CONFIGURED.replace("JetsonIO", "custom-audio"),
        CONFIGURED.replace("DEFAULT JetsonIO", "  default\tJetsonIO # selected entry"),
        CONFIGURED.replace("LABEL", "label").replace("FDT", "fdt"),
        CONFIGURED.replace("\n", "\r\n"),
        "# DEFAULT primary\n" + CONFIGURED + "\n# LABEL backup\n",
        # Direct overlay paths do not need Jetson-IO's descriptive menu title.
        "DEFAULT primary\n" + PRIMARY + "    OVERLAYS /boot/camera.dtbo,/boot/adafruit-uda1334a.dtbo\n",
        "DEFAULT primary\n" + PRIMARY + "    FDTOVERLAYS /boot/adafruit-uda1334a.dtbo /boot/camera.dtbo\n",
        "DEFAULT primary\n" + PRIMARY + "    FDT /boot/Adafruit-UDA1334A.dtb\n",
    ],
    ids=[
        "jetson-io",
        "first-entry",
        "custom-label",
        "whitespace",
        "lowercase-directives",
        "crlf",
        "commented-labels",
        "overlays",
        "fdtoverlays",
        "named-dtb",
    ],
)
def test_selected_audio_entry_is_left_unchanged(audio_setup, contents):
    output, calls, config = audio_setup(contents)
    assert calls == []
    assert config.read_bytes() == contents.encode()
    assert "already configured in the default boot entry" in output
    assert "reboot=false" in output


@pytest.mark.parametrize(
    "contents",
    [
        RESET,
        "DEFAULT primary\n" + JETSON_IO + PRIMARY,
        CONFIGURED.replace("DEFAULT JetsonIO", "DEFAULT Jetson"),
        CONFIGURED.replace("DEFAULT JetsonIO", "DEFAULT jetsonio"),
        PRIMARY + JETSON_IO,
        "DEFAULT missing\n" + PRIMARY + JETSON_IO,
        "# DEFAULT JetsonIO\n" + PRIMARY + JETSON_IO,
        "DEFAULT primary\n" + PRIMARY + "    # OVERLAYS /boot/adafruit-uda1334a.dtbo\n",
        "DEFAULT primary\n" + PRIMARY + "    FDT /boot/base.dtb # UDA1334A\n",
        CONFIGURED.replace("    FDT /boot/kernel_tegra234-p3767-user-custom.dtb\n", ""),
        "DEFAULT primary\n" + PRIMARY + "    APPEND hardware=UDA1334A\n",
        "DEFAULT primary\n" + PRIMARY + "    MENU LABEL UDA1334A\n" + JETSON_IO,
        "DEFAULT primary\n" + CONFIGURED,
        CONFIGURED + JETSON_IO,
        "",
        None,
    ],
    ids=[
        "nvidia-reset",
        "inactive-audio-first",
        "label-prefix",
        "label-case",
        "missing-default",
        "unknown-default",
        "commented-default",
        "commented-overlay",
        "inline-comment",
        "title-without-fdt",
        "kernel-argument",
        "fdt-in-other-entry",
        "ambiguous-default",
        "ambiguous-label",
        "empty-file",
        "missing-file",
    ],
)
def test_missing_selected_audio_is_repaired(audio_setup, contents):
    output, calls, config = audio_setup(contents)
    assert calls == ["-n", "Adafruit UDA1334A"]
    assert config.read_text() == CONFIGURED
    assert "configured successfully in the default boot entry" in output
    assert "reboot=true" in output
    assert "WARNING" not in output


@pytest.mark.parametrize("repair", ["noop", "fail"])
def test_failed_repair_is_not_reported_as_success(audio_setup, repair):
    output, calls, config = audio_setup(RESET, repair=repair)
    assert calls == ["-n", "Adafruit UDA1334A"]
    assert config.read_text() == RESET
    assert "WARNING" in output
    assert "configured successfully" not in output
    assert "reboot=false" in output


def test_non_jetson_skips_configuration(audio_setup):
    output, calls, _ = audio_setup(None, jetson=False)
    assert calls == []
    assert "Skipping I2S config - not a Jetson device" in output
    assert "reboot=false" in output


def test_script_parses():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True, capture_output=True)
