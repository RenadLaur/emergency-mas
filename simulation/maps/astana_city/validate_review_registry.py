"""Validate the manual-review registry without claiming unreviewed geometry is true."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


BASE = Path(__file__).resolve().parent
REGISTRY = BASE / "review_registry.json"
OUT = BASE / "generated" / "review_registry_report.json"
STATUSES = {"inventory_pending", "requires_manual_review", "modelled_assumption", "verified_geometry"}
REQUIRED_FIELDS = {"id", "title", "kind", "status", "center", "osm_way_ids", "checks", "evidence", "open_questions"}


def main() -> None:
    data = json.loads(REGISTRY.read_text())
    seen_ids = set()
    counts: Counter[str] = Counter()
    errors = []

    for entry in data.get("entries", []):
        missing = sorted(REQUIRED_FIELDS - entry.keys())
        if missing:
            errors.append(f"{entry.get('id', '<missing id>')}: missing fields {missing}")
            continue
        if entry["id"] in seen_ids:
            errors.append(f"duplicate id: {entry['id']}")
        seen_ids.add(entry["id"])
        if entry["status"] not in STATUSES:
            errors.append(f"{entry['id']}: unsupported status {entry['status']}")
        if entry["status"] != "inventory_pending" and entry["center"] is None:
            errors.append(f"{entry['id']}: located entries require a center")
        if entry["status"] == "verified_geometry":
            if not entry.get("reviewed_by") or not entry.get("reviewed_at"):
                errors.append(f"{entry['id']}: verified geometry requires reviewer and review date")
            if not entry["evidence"]:
                errors.append(f"{entry['id']}: verified geometry requires evidence")
        counts[entry["status"]] += 1

    report = {
        "registry": REGISTRY.name,
        "summary": {
            "entries": len(data.get("entries", [])),
            "by_status": dict(sorted(counts.items())),
            "valid": not errors,
            "errors": errors,
        },
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report["summary"], ensure_ascii=False))
    if errors:
        raise SystemExit("Registry validation failed")


if __name__ == "__main__":
    main()
