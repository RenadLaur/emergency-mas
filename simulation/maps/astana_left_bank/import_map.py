"""Import the Nura and Esil districts as the Astana left-bank SUMO network."""

from __future__ import annotations

import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

import sumo


MAP_DIR = Path(__file__).resolve().parent
RAW_DIR = MAP_DIR / "raw"
GENERATED_DIR = MAP_DIR / "generated"
AREA_FILE = MAP_DIR / "area.json"
OVERRIDES_FILE = MAP_DIR / "osm_overrides.json"
PREFIX = "astana_left_bank"


def sumo_root() -> Path:
    return Path(sumo.__file__).resolve().parent


def apply_osm_overrides(raw_files: list[Path]) -> tuple[list[Path], int]:
    """Apply reviewed OSM tag corrections to derived copies, never to raw snapshots."""
    payload = json.loads(OVERRIDES_FILE.read_text(encoding="utf-8"))
    overrides = {item["osm_way_id"]: item for item in payload["way_overrides"]}
    matched: set[str] = set()
    output_dir = GENERATED_DIR / "overridden_osm"
    output_dir.mkdir(exist_ok=True)
    output_files: list[Path] = []

    for raw_file in raw_files:
        tree = ET.parse(raw_file)
        root = tree.getroot()
        for way in root.findall("way"):
            way_id = way.attrib["id"]
            override = overrides.get(way_id)
            if override is None:
                continue
            tags = {tag.attrib["k"]: tag for tag in way.findall("tag")}
            for key, value in override["tags"].items():
                if value is None:
                    if key in tags:
                        way.remove(tags[key])
                elif key in tags:
                    tags[key].set("v", value)
                else:
                    ET.SubElement(way, "tag", {"k": key, "v": value})
            matched.add(way_id)

        output_file = output_dir / raw_file.name
        tree.write(output_file, encoding="utf-8", xml_declaration=True)
        output_files.append(output_file)

    missing = sorted(set(overrides) - matched)
    if missing:
        raise ValueError(f"OSM overrides did not match downloaded ways: {missing}")
    return output_files, len(matched)


def run() -> None:
    area = json.loads(AREA_FILE.read_text(encoding="utf-8"))
    root = sumo_root()
    osm_get = root / "tools" / "osmGet.py"
    osm_build = root / "tools" / "osmBuild.py"
    typemap_dir = root / "data" / "typemap"

    RAW_DIR.mkdir(exist_ok=True)
    GENERATED_DIR.mkdir(exist_ok=True)

    raw_files: list[Path] = []
    for relation in area["osm_relations"]:
        relation_prefix = relation["name"].split()[0].lower()
        raw_file = RAW_DIR / f"{relation_prefix}_city.osm.xml"
        raw_files.append(raw_file)
        if raw_file.exists():
            continue
        subprocess.run(
            [
                sys.executable,
                str(osm_get),
                "--area",
                str(relation["relation_id"]),
                "--prefix",
                relation_prefix,
                "--output-dir",
                str(RAW_DIR),
                "--retries",
                "5",
                "--retry-delay",
                "10",
                "--verbose",
            ],
            check=True,
        )
        if not raw_file.exists():
            raise FileNotFoundError(f"Expected OSM output is missing: {raw_file}")

    build_files, applied_override_count = apply_osm_overrides(raw_files)

    netconvert_options = ",".join(
        [
            "--geometry.remove",
            "--roundabouts.guess",
            "--osm.turn-lanes",
            "--osm.lane-access",
            "--junctions.corner-detail,10",
            "--junctions.internal-link-detail,10",
            "--junctions.join",
            "--tls.guess-signals",
            "--tls.discard-simple",
            "--tls.join",
            "--tls.green.time,20",
            "--tls.yellow.time,3",
            "--output.original-names",
            "--output.street-names",
        ]
    )
    subprocess.run(
        [
            sys.executable,
            str(osm_build),
            "--osm-file",
            ",".join(str(path) for path in build_files),
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
            f"--netconvert-options={netconvert_options}",
            "--verbose",
        ],
        check=True,
    )

    manifest = {
        "area": area,
        "downloaded_at_utc": datetime.now(UTC).isoformat(),
        "raw_files": [str(path.relative_to(MAP_DIR)) for path in raw_files],
        "build_files": [str(path.relative_to(MAP_DIR)) for path in build_files],
        "osm_override_file": str(OVERRIDES_FILE.relative_to(MAP_DIR)),
        "applied_osm_way_override_count": applied_override_count,
        "generated_files": sorted(path.name for path in GENERATED_DIR.iterdir()),
        "sumo_package_root": str(root),
        "note": "Buildings are intentionally deferred; the first full-area build is road-focused.",
    }
    (MAP_DIR / "import_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    run()
