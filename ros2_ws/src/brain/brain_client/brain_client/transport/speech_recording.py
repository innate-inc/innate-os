"""Best-effort MP3 archive of PCM sent to the robot speaker."""
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import tempfile

# Ten minutes at 16 kHz, mono, signed 16-bit. Bound memory on runaway streams.
MAX_PCM_BYTES = 16000 * 2 * 60 * 10


def save_speech(pcm, sample_rate, logger, directory=None):
    """Archive a clip; disk/encoder failure must never fail speech playback."""
    if not pcm:
        return None
    temporary = None
    try:
        directory = Path(directory) if directory is not None else Path.home() / "speech-recordings"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S_%fZ_")
        with tempfile.NamedTemporaryFile(prefix=stamp, suffix=".part", dir=directory, delete=False) as f:
            temporary = Path(f.name)
        subprocess.run(
            ["lame", "--silent", "-r", "-s", str(sample_rate / 1000),
             "--bitwidth", "16", "--signed", "--little-endian", "-m", "m",
             "-b", "64", "-", str(temporary)],
            input=pcm, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            check=True, timeout=15,
        )
        if temporary.stat().st_size == 0:
            raise RuntimeError("MP3 encoder produced no audio")
        output = temporary.with_suffix(".mp3")
        temporary.replace(output)
        logger.info(f"Speech recording saved: {output}")
        return output
    except Exception as exc:
        logger.warning(f"Speech recording failed (playback unaffected): {exc}")
        return None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
