"""Build the generated SUMO network from versioned XML source files."""

from __future__ import annotations

import subprocess
from pathlib import Path

import sumolib


SCENARIO_DIR = Path(__file__).resolve().parent
NETWORK_FILE = SCENARIO_DIR / "basic.net.xml"


def build_network() -> Path:
    """Generate the network and return the path to it."""
    netconvert = sumolib.checkBinary("netconvert")

    subprocess.run(
        [
            netconvert,
            "--node-files",
            str(SCENARIO_DIR / "basic.nod.xml"),
            "--edge-files",
            str(SCENARIO_DIR / "basic.edg.xml"),
            "--connection-files",
            str(SCENARIO_DIR / "basic.con.xml"),
            "--output-file",
            str(NETWORK_FILE),
            "--tls.green.time",
            "20",
            "--tls.yellow.time",
            "3",
        ],
        check=True,
    )
    return NETWORK_FILE


if __name__ == "__main__":
    print(build_network())
