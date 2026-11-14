"""Build the three-lane corridor used by the incident-response experiment."""
from __future__ import annotations

import subprocess
from pathlib import Path

import sumolib


SCENARIO_DIR = Path(__file__).resolve().parent
NETWORK_FILE = SCENARIO_DIR / "intersection_incident_response.net.xml"


def build_network() -> Path:
    """Generate an isolated network so this experiment cannot alter other demos."""
    netconvert = sumolib.checkBinary("netconvert")
    subprocess.run(
        [
            netconvert,
            "--node-files", str(SCENARIO_DIR / "intersection_incident_response.nod.xml"),
            "--edge-files", str(SCENARIO_DIR / "intersection_incident_response.edg.xml"),
            "--output-file", str(NETWORK_FILE),
            "--tls.green.time", "16",
            "--tls.yellow.time", "3",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return NETWORK_FILE
