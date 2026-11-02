"""Build the deterministic two-lane rescue-corridor SUMO network."""
from __future__ import annotations

import subprocess
from pathlib import Path

import sumolib


SCENARIO_DIR = Path(__file__).resolve().parent
NETWORK_FILE = SCENARIO_DIR / "rescue_lane.net.xml"


def build_network() -> Path:
    netconvert = sumolib.checkBinary("netconvert")
    subprocess.run([netconvert, "--node-files", str(SCENARIO_DIR / "rescue_lane.nod.xml"),
                    "--edge-files", str(SCENARIO_DIR / "rescue_lane.edg.xml"),
                    "--connection-files", str(SCENARIO_DIR / "rescue_lane.con.xml"),
                    "--output-file", str(NETWORK_FILE)], check=True)
    return NETWORK_FILE


if __name__ == "__main__":
    print(build_network())
