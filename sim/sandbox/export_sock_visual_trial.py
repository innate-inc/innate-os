"""Export recorded cloth positions for the viewer-only visual regression page."""
import argparse
import json
from pathlib import Path

import numpy as np

parser = argparse.ArgumentParser()
parser.add_argument("trajectory", type=Path)
parser.add_argument("output", type=Path)
args = parser.parse_args()
with np.load(args.trajectory) as recorded:
    frames = recorded["vertices"]
    payload = {"frames": frames.tolist(), "faces": recorded["faces"].tolist()}
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(payload))
