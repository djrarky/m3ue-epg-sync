"""
reporter - Write IPTV-centric CSVs, source_map.yaml, and summary.json.

All reports are written to ./out/<uuid>/ (one subdirectory per playlist).
"""
from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    import sys
    sys.exit("Missing dependency: pip install pyyaml")

from iptv import group_name
from matcher import SourceEntry, MatchResult

logger = logging.getLogger(__name__)


def _write_csv(
    path: Path,
    fieldnames: list[str],
    rows: list[Any],
    row_fn: callable,
) -> None:
    """Write a CSV file with the given fieldnames and row generator."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for item in rows:
            writer.writerow(row_fn(item))


def write_reports(
    output_dir: Path,
    source_entries: list[SourceEntry],
    match_results: list[tuple[dict, MatchResult]],
    excluded_channels: list[tuple[dict, str]],
    guide_map: dict[int, str],
) -> dict:
    """Write all match reports and return the summary dict."""
    output_dir.mkdir(parents=True, exist_ok=True)

    # Partition match results by decision
    auto_apply: list[tuple[dict, MatchResult]] = []
    needs_review: list[tuple[dict, MatchResult]] = []
    unmatched: list[tuple[dict, MatchResult]] = []

    for ch, result in match_results:
        if result.decision in ("auto_apply", "fingerprint"):
            auto_apply.append((ch, result))
        elif result.decision == "needs_review":
            needs_review.append((ch, result))
        else:
            unmatched.append((ch, result))

    def _channel_row(ch: dict) -> dict:
        """Common IPTV channel fields for all CSV reports."""
        return {
            "iptv_id": ch.get("id", ""),
            "iptv_title": ch.get("title", ""),
            "iptv_group": group_name(ch),
        }

    def _match_row(ch: dict, result: MatchResult, prefix: str = "") -> dict:
        """Common channel + match fields."""
        row = _channel_row(ch)
        src = result.source
        row[f"{prefix}source_number"] = src.number if src else ""
        row[f"{prefix}source_name"] = src.name if src else ""
        row[f"{prefix}stream_id"] = guide_map.get(src.number, "") if src else ""
        row["category"] = src.category if src else ""
        row["tier"] = result.tier or ""
        row["score"] = result.score
        return row

    # ── auto_apply.csv ───────────────────────────────────────────────────
    _write_csv(
        output_dir / "auto_apply.csv",
        ["iptv_id", "iptv_title", "iptv_group", "source_number",
         "source_name", "stream_id", "category", "tier", "score", "decision"],
        auto_apply,
        lambda item: {**_match_row(item[0], item[1]), "decision": item[1].decision},
    )

    # ── needs_review.csv ─────────────────────────────────────────────────
    def _needs_review_row(item: tuple[dict, MatchResult]) -> dict:
        ch, result = item
        row = _match_row(ch, result, prefix="best_")
        row["candidates"] = "; ".join(
            f"{e.name} ({e.number}) score={s}" for e, s in result.candidates
        ) if result.candidates else ""
        return row

    _write_csv(
        output_dir / "needs_review.csv",
        ["iptv_id", "iptv_title", "iptv_group", "best_source_number",
         "best_source_name", "best_stream_id", "category", "tier", "score", "candidates"],
        needs_review,
        _needs_review_row,
    )

    # ── unmatched.csv ────────────────────────────────────────────────────
    _write_csv(
        output_dir / "unmatched.csv",
        ["iptv_id", "iptv_title", "iptv_group"],
        unmatched,
        lambda item: _channel_row(item[0]),
    )

    # ── excluded.csv ─────────────────────────────────────────────────────
    _write_csv(
        output_dir / "excluded.csv",
        ["iptv_id", "iptv_title", "iptv_group", "reason"],
        excluded_channels,
        lambda item: {**_channel_row(item[0]), "reason": item[1]},
    )

    # ── source_map.yaml ──────────────────────────────────────────────────
    _write_source_map(output_dir / "source_map.yaml", source_entries, match_results)

    # ── summary.json ─────────────────────────────────────────────────────
    matched_numbers = {r.source.number for _, r in match_results if r.source}

    summary = {
        "matching": {
            "auto_apply": len(auto_apply),
            "needs_review": len(needs_review),
            "unmatched": len(unmatched),
            "excluded": len(excluded_channels),
            "total_in_scope": len(match_results),
            "source_total": len(source_entries),
            "source_matched": len(matched_numbers),
            "source_no_iptv_match": sum(1 for e in source_entries if e.number not in matched_numbers),
        },
    }

    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )

    logger.info("Reports written to %s", output_dir)
    return summary


def update_summary_writes(output_dir: Path, write_summary: dict) -> None:
    """Add the writes section to an existing summary.json."""
    summary_path = output_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["writes"] = write_summary
    summary_path.write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )


def _write_source_map(
    path: Path,
    source_entries: list[SourceEntry],
    match_results: list[tuple[dict, MatchResult]],
) -> None:
    """Write source_map.yaml - source-centric view of matching coverage."""
    source_matches: dict[int, list[dict]] = {}
    for ch, result in match_results:
        if result.source:
            source_matches.setdefault(result.source.number, []).append({
                "iptv_id": ch.get("id", ""),
                "iptv_title": ch.get("title", ""),
                "tier": result.tier,
                "score": result.score,
                "decision": result.decision,
            })

    source_map: list[dict] = []
    for entry in source_entries:
        record: dict = {
            "number": entry.number,
            "name": entry.name,
            "category": entry.category,
        }
        if entry.number in source_matches:
            record["iptv_matches"] = source_matches[entry.number]
        else:
            record["iptv_matches"] = []
            record["no_iptv_match"] = True
        source_map.append(record)

    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(source_map, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
