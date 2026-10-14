"""Smoke tests for the local development environment."""

from __future__ import annotations

import shutil
import subprocess

import networkx
import numpy
import osmnx
import pandas
import sumolib
import traci


def test_required_python_modules_import() -> None:
    assert all((networkx, numpy, osmnx, pandas, sumolib, traci))


def test_sumo_tools_are_on_path() -> None:
    for tool in ("sumo", "netconvert", "duarouter"):
        executable = shutil.which(tool)
        assert executable is not None, f"{tool} is not on PATH; activate emergency-mas"
        result = subprocess.run(
            [executable, "--version"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert "Eclipse SUMO" in result.stdout
