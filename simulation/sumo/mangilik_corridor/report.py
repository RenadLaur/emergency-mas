"""Build reproducible tables and figures from a Mangilik experiment matrix."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


MODE_LABELS = {
    "normal": "Normal",
    "priority": "Signal priority",
    "yielding": "Civilian yielding",
    "assisted": "Combined",
}
MODE_COLORS = {
    "normal": "#6b7280",
    "priority": "#2563eb",
    "yielding": "#f59e0b",
    "assisted": "#16a34a",
}


def load_matrix(path: Path) -> dict[str, Any]:
    matrix = json.loads(path.read_text(encoding="utf-8"))
    required = {"seeds", "civilian_counts", "modes", "aggregates_by_civilian_count"}
    missing = required - matrix.keys()
    if missing:
        raise ValueError(f"Matrix has no fields: {sorted(missing)}")
    return matrix


def summary_rows(matrix: dict[str, Any]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    aggregates = matrix["aggregates_by_civilian_count"]
    for civilian_count in matrix["civilian_counts"]:
        demand = aggregates[str(civilian_count)]
        for mode in matrix["modes"]:
            mode_result = demand["modes"][mode]
            row: dict[str, object] = {
                "civilian_count": civilian_count,
                "mode": mode,
                "runs": mode_result["runs"],
                "ambulance_success_rate": mode_result["ambulance_success_rate"],
            }
            for metric, values in mode_result["metrics"].items():
                row[f"{metric}_mean"] = values["mean"]
                row[f"{metric}_sample_std"] = values["sample_std"]
            rows.append(row)
    return rows


def effect_rows(matrix: dict[str, Any]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    aggregates = matrix["aggregates_by_civilian_count"]
    for civilian_count in matrix["civilian_counts"]:
        effects = aggregates[str(civilian_count)]["effects_against_normal"]
        for mode, metrics in effects.items():
            row: dict[str, object] = {"civilian_count": civilian_count, "mode": mode}
            for metric, values in metrics.items():
                row[f"{metric}_mean"] = values["mean"]
                row[f"{metric}_sample_std"] = values["sample_std"]
            rows.append(row)
    return rows


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise ValueError("Cannot write an empty report table")
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _plot_metric(
    matrix: dict[str, Any], metric: str, ylabel: str, output_path: Path
) -> None:
    counts = matrix["civilian_counts"]
    aggregates = matrix["aggregates_by_civilian_count"]
    figure, axis = plt.subplots(figsize=(9, 5.4))
    for mode in matrix["modes"]:
        means = [aggregates[str(count)]["modes"][mode]["metrics"][metric]["mean"] for count in counts]
        deviations = [
            aggregates[str(count)]["modes"][mode]["metrics"][metric]["sample_std"]
            for count in counts
        ]
        axis.errorbar(
            counts,
            means,
            yerr=deviations,
            marker="o",
            linewidth=2,
            capsize=4,
            label=MODE_LABELS[mode],
            color=MODE_COLORS[mode],
        )
    axis.set_xlabel("Civilian vehicles")
    axis.set_ylabel(ylabel)
    axis.grid(alpha=0.25)
    axis.legend()
    figure.tight_layout()
    figure.savefig(output_path, dpi=180)
    plt.close(figure)


def _plot_safety(matrix: dict[str, Any], output_path: Path) -> None:
    counts = matrix["civilian_counts"]
    aggregates = matrix["aggregates_by_civilian_count"]
    figure, axes = plt.subplots(1, 2, figsize=(12, 5.2))
    for mode in matrix["modes"]:
        near_misses = [
            aggregates[str(count)]["modes"][mode]["metrics"]["near_misses"]["mean"]
            for count in counts
        ]
        axes[0].plot(
            counts,
            near_misses,
            marker="o",
            linewidth=2,
            label=MODE_LABELS[mode],
            color=MODE_COLORS[mode],
        )

    positions = list(range(len(counts)))
    width = 0.18
    mode_count = len(matrix["modes"])
    for index, mode in enumerate(matrix["modes"]):
        collisions = [
            aggregates[str(count)]["modes"][mode]["metrics"]["physical_collisions"][
                "mean"
            ]
            for count in counts
        ]
        offsets = [
            position + (index - (mode_count - 1) / 2) * width for position in positions
        ]
        axes[1].bar(
            offsets,
            collisions,
            width,
            label=MODE_LABELS[mode],
            color=MODE_COLORS[mode],
        )

    axes[0].set_title("Near-miss events")
    axes[1].set_title("Physical collisions")
    axes[1].set_xticks(positions, [str(count) for count in counts])
    for axis in axes:
        axis.set_xlabel("Civilian vehicles")
        axis.grid(axis="y", alpha=0.25)
    axes[0].set_ylabel("Mean events per run")
    axes[0].legend()
    axes[1].legend()
    figure.tight_layout()
    figure.savefig(output_path, dpi=180)
    plt.close(figure)


def _plot_effects(matrix: dict[str, Any], output_path: Path) -> None:
    counts = matrix["civilian_counts"]
    modes = [mode for mode in matrix["modes"] if mode != "normal"]
    aggregates = matrix["aggregates_by_civilian_count"]
    width = 0.23
    positions = list(range(len(counts)))
    figure, axis = plt.subplots(figsize=(9, 5.4))
    for index, mode in enumerate(modes):
        values = [
            aggregates[str(count)]["effects_against_normal"][mode][
                "ambulance_response_time_delta_seconds"
            ]["mean"]
            for count in counts
        ]
        offsets = [position + (index - 1) * width for position in positions]
        axis.bar(offsets, values, width, label=MODE_LABELS[mode], color=MODE_COLORS[mode])
    axis.axhline(0, color="#111827", linewidth=1)
    axis.set_xticks(positions, [str(count) for count in counts])
    axis.set_xlabel("Civilian vehicles")
    axis.set_ylabel("Response-time delta vs normal, seconds")
    axis.text(
        0.01, 0.02, "Negative values are better", transform=axis.transAxes, color="#374151"
    )
    axis.grid(axis="y", alpha=0.25)
    axis.legend()
    figure.tight_layout()
    figure.savefig(output_path, dpi=180)
    plt.close(figure)


def _markdown_report(matrix: dict[str, Any]) -> str:
    aggregates = matrix["aggregates_by_civilian_count"]
    lines = [
        "# Mangilik corridor experiment report",
        "",
        f"Seeds: `{', '.join(map(str, matrix['seeds']))}`. "
        f"Runs: `{len(matrix['seeds']) * len(matrix['civilian_counts']) * len(matrix['modes'])}`.",
        f"Each cell contains {len(matrix['seeds'])} paired runs. "
        "Values are mean ± sample standard deviation.",
        "",
        "| Demand | Mode | Ambulance, s | Civilian time loss, s | Near-miss | Collisions |",
        "|---:|---|---:|---:|---:|---:|",
    ]
    for count in matrix["civilian_counts"]:
        for mode in matrix["modes"]:
            metrics = aggregates[str(count)]["modes"][mode]["metrics"]
            def formatted(metric: str) -> str:
                values = metrics[metric]
                return f"{values['mean']:.2f} ± {values['sample_std']:.2f}"
            lines.append(
                f"| {count} | {MODE_LABELS[mode]} | "
                f"{formatted('ambulance_response_time_seconds')} | "
                f"{formatted('civilian_mean_time_loss_seconds')} | "
                f"{formatted('near_misses')} | {formatted('physical_collisions')} |"
            )

    lines.extend([
        "",
        "## Preliminary interpretation",
        "",
    ])
    for mode in matrix["modes"]:
        if mode == "normal":
            continue
        deltas = [
            aggregates[str(count)]["effects_against_normal"][mode][
                "ambulance_response_time_delta_seconds"
            ]["mean"]
            for count in matrix["civilian_counts"]
        ]
        collision_total = sum(
            aggregates[str(count)]["modes"][mode]["metrics"]["physical_collisions"][
                "mean"
            ]
            * aggregates[str(count)]["modes"][mode]["runs"]
            for count in matrix["civilian_counts"]
        )
        lines.append(
            f"- {MODE_LABELS[mode]}: response-time delta ranges from "
            f"{min(deltas):+.2f} to {max(deltas):+.2f} s; "
            f"physical collisions across the matrix: {collision_total:.0f}."
        )
    lines.extend([
        f"- {len(matrix['seeds'])} seeds are sufficient for an engineering smoke study, not for a final significance "
        "claim. The diploma experiment should add seeds and report confidence intervals.",
        "",
        "## Files",
        "",
        "- `summary.csv` — absolute metrics by demand and mode.",
        "- `effects.csv` — paired deltas against normal mode.",
        "- `ambulance_response.png`, `civilian_time_loss.png`, `safety.png`, "
        "`ambulance_effects.png` — figures generated from the same JSON.",
        "",
    ])
    return "\n".join(lines)


def build_report(matrix_path: Path, output_dir: Path) -> dict[str, str]:
    matrix = load_matrix(matrix_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "summary.csv", summary_rows(matrix))
    _write_csv(output_dir / "effects.csv", effect_rows(matrix))
    _plot_metric(
        matrix,
        "ambulance_response_time_seconds",
        "Ambulance response time, seconds",
        output_dir / "ambulance_response.png",
    )
    _plot_metric(
        matrix,
        "civilian_mean_time_loss_seconds",
        "Mean civilian time loss, seconds",
        output_dir / "civilian_time_loss.png",
    )
    _plot_safety(matrix, output_dir / "safety.png")
    _plot_effects(matrix, output_dir / "ambulance_effects.png")
    (output_dir / "REPORT.md").write_text(_markdown_report(matrix), encoding="utf-8")
    return {
        "report": str(output_dir / "REPORT.md"),
        "summary_csv": str(output_dir / "summary.csv"),
        "effects_csv": str(output_dir / "effects.csv"),
        "figures": str(output_dir),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("matrix", type=Path, help="path to batch_summary.json")
    parser.add_argument("--output-dir", type=Path, required=True)
    arguments = parser.parse_args()
    print(json.dumps(build_report(arguments.matrix, arguments.output_dir), indent=2))
