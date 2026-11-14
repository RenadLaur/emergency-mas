"""Check passenger-car reachability between representative Astana locations.

This is a topology check for the complete imported network.  It does not certify
lane markings, bridge elevations, signal phases, or real-world travel times.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import sumolib


BASE = Path(__file__).resolve().parent
OUT = BASE / "generated"
NETWORK = OUT / "astana_city.net.xml"
REPORT = OUT / "coverage_routes.json"


@dataclass(frozen=True)
class Anchor:
    """Named point placed near a major road or city area, in WGS84 coordinates."""

    name: str
    longitude: float
    latitude: float


# A compact city-wide smoke matrix: both banks, major radial roads and the
# airport direction.  Coordinates deliberately identify areas, not a claim
# that the named road has been manually calibrated at this exact point.
ANCHORS = (
    Anchor("center_right_bank", 71.4230, 51.1650),
    Anchor("center_left_bank", 71.4305, 51.1283),
    Anchor("mangilik_el", 71.4460, 51.1110),
    Anchor("turan", 71.4080, 51.0870),
    Anchor("uly_dala", 71.4690, 51.0930),
    Anchor("alash", 71.4700, 51.2180),
    Anchor("ablaikhan", 71.4580, 51.1950),
    Anchor("airport_direction", 71.4730, 51.0370),
    Anchor("west_corridor", 71.3500, 51.1370),
)


def nearest_passenger_edge(net: sumolib.net.Net, anchor: Anchor):
    """Return the nearest usable edge and its distance from a named anchor."""
    x, y = net.convertLonLat2XY(anchor.longitude, anchor.latitude)
    candidates = [
        (distance, edge)
        for edge, distance in net.getNeighboringEdges(x, y, 2_000)
        if edge.allows("passenger") and not edge.isSpecial()
    ]
    if not candidates:
        raise ValueError(f"No passenger edge within 2 km of {anchor.name}")
    distance, edge = min(candidates, key=lambda candidate: (candidate[0], candidate[1].getID()))
    return edge, round(distance, 2)


def main() -> None:
    if not NETWORK.exists():
        raise FileNotFoundError(f"Build the city network first: {NETWORK}")

    net = sumolib.net.readNet(str(NETWORK))
    resolved = {}
    for anchor in ANCHORS:
        edge, distance = nearest_passenger_edge(net, anchor)
        resolved[anchor.name] = {"edge_id": edge.getID(), "distance_m": distance, "edge": edge}

    hub = "center_left_bank"
    checks = []
    failures = []
    for name, item in resolved.items():
        if name == hub:
            continue
        for start_name, end_name in ((hub, name), (name, hub)):
            path, cost = net.getFastestPath(
                resolved[start_name]["edge"],
                resolved[end_name]["edge"],
                vClass="passenger",
            )
            check = {
                "from": start_name,
                "to": end_name,
                "reachable": path is not None,
                "edge_count": len(path) if path else 0,
                "free_flow_seconds": round(cost, 2) if cost is not None else None,
            }
            checks.append(check)
            if path is None:
                failures.append(check)

    report = {
        "purpose": "City-wide passenger-car connectivity smoke check, not geometric certification.",
        "network": str(NETWORK.name),
        "hub": hub,
        "anchors": {
            name: {key: value for key, value in item.items() if key != "edge"}
            for name, item in resolved.items()
        },
        "checks": checks,
        "summary": {
            "anchor_count": len(resolved),
            "route_checks": len(checks),
            "unreachable": len(failures),
            "passed": not failures,
        },
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report["summary"], ensure_ascii=False))
    if failures:
        raise SystemExit(f"Unreachable city coverage routes: {failures}")


if __name__ == "__main__":
    main()
