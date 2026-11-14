"""Build a two-lane signalized intersection for controlled agent experiments."""
from __future__ import annotations

import subprocess
from pathlib import Path

import sumolib


SCENARIO_DIR = Path(__file__).resolve().parent
NETWORK_FILE = SCENARIO_DIR / "intelligent_intersection.net.xml"


def build_network() -> Path:
    """Generate the network from the versioned node and edge definitions."""
    netconvert = sumolib.checkBinary("netconvert")
    subprocess.run(
        [
            netconvert,
            "--node-files", str(SCENARIO_DIR / "intelligent_intersection.nod.xml"),
            "--edge-files", str(SCENARIO_DIR / "intelligent_intersection.edg.xml"),
            "--output-file", str(NETWORK_FILE),
            "--tls.green.time", "16",
            "--tls.yellow.time", "3",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return NETWORK_FILE
