"""Download and convert the Astana pilot area from OpenStreetMap into SUMO files."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import sumo


MAP_DIR = Path(__file__).resolve().parent
RAW_DIR = MAP_DIR / "raw"
GENERATED_DIR = MAP_DIR / "generated"
AREA_FILE = MAP_DIR / "area.json"
PREFIX = "astana_pilot"


def sumo_root() -> Path:
    return Path(sumo.__file__).resolve().parent


def run() -> None:
    area = json.loads(AREA_FILE.read_text(encoding="utf-8"))
    bbox = ",".join(str(value) for value in area["bbox_west_south_east_north"])
    root = sumo_root()
    osm_get = root / "tools" / "osmGet.py"
    osm_build = root / "tools" / "osmBuild.py"
    typemap_dir = root / "data" / "typemap"

    RAW_DIR.mkdir(exist_ok=True)
    GENERATED_DIR.mkdir(exist_ok=True)
    # osmGet appends `_bbox` for a bounding-box request.
    raw_file = RAW_DIR / f"{PREFIX}_bbox.osm.xml"

    if not raw_file.exists():
        subprocess.run(
            [
                sys.executable,
                str(osm_get),
                "--bbox",
                bbox,
                "--prefix",
                PREFIX,
                "--output-dir",
                str(RAW_DIR),
                "--shapes",
                "--retries",
                "3",
                "--retry-delay",
                "5",
                "--verbose",
            ],
            check=True,
        )
    if not raw_file.exists():
        raise FileNotFoundError(f"Expected OSM output is missing: {raw_file}")

    netconvert_options = ",".join(
        [
            "--geometry.remove",
            "--roundabouts.guess",
            "--ramps.guess",
            "--junctions.join",
            "--tls.guess-signals",
            "--tls.discard-simple",
            "--tls.join",
            "--tls.green.time,20",
            "--tls.yellow.time,3",
        ]
    )
    subprocess.run(
        [
            sys.executable,
            str(osm_build),
            "--osm-file",
            str(raw_file),
            "--prefix",
            PREFIX,
            "--output-directory",
            str(GENERATED_DIR),
            "--vehicle-classes",
            "road",
            "--netconvert-typemap",
            ",".join(
                [
                    str(typemap_dir / "osmNetconvert.typ.xml"),
                    str(typemap_dir / "osmNetconvertUrbanDe.typ.xml"),
                ]
            ),
            "--typemap",
            str(typemap_dir / "osmPolyconvert.typ.xml"),
            f"--netconvert-options={netconvert_options}",
            "--verbose",
        ],
        check=True,
    )

    manifest = {
        "area": area,
        "downloaded_at_utc": datetime.now(UTC).isoformat(),
        "raw_file": str(raw_file.relative_to(MAP_DIR)),
        "generated_files": sorted(path.name for path in GENERATED_DIR.iterdir()),
        "sumo_package_root": str(root),
    }
    (MAP_DIR / "import_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


if __name__ == "__main__":
    run()
