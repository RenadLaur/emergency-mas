"""Build the minimal network used to observe a physical rear-end collision."""
from __future__ import annotations

import subprocess
from pathlib import Path

import sumolib


SCENARIO_DIR = Path(__file__).resolve().parent
NETWORK_FILE = SCENARIO_DIR / "collision_response.net.xml"


def build_network() -> Path:
    netconvert = sumolib.checkBinary("netconvert")
    subprocess.run([
        netconvert,
        "--node-files", str(SCENARIO_DIR / "collision_response.nod.xml"),
        "--edge-files", str(SCENARIO_DIR / "collision_response.edg.xml"),
        "--output-file", str(NETWORK_FILE),
    ], check=True, capture_output=True, text=True)
    return NETWORK_FILE
