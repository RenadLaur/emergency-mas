"""Build the two-route network used to verify emergency rerouting."""
from __future__ import annotations

import subprocess
from pathlib import Path

import sumolib


SCENARIO_DIR = Path(__file__).resolve().parent
NETWORK_FILE = SCENARIO_DIR / "incident_rerouting.net.xml"


def build_network() -> Path:
    netconvert = sumolib.checkBinary("netconvert")
    subprocess.run([netconvert, "--node-files", str(SCENARIO_DIR / "incident_rerouting.nod.xml"),
                    "--edge-files", str(SCENARIO_DIR / "incident_rerouting.edg.xml"),
                    "--connection-files", str(SCENARIO_DIR / "incident_rerouting.con.xml"),
                    "--output-file", str(NETWORK_FILE)], check=True)
    return NETWORK_FILE


if __name__ == "__main__":
    print(build_network())
