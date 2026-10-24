"""Lossless sharing of repeated observation dates in analytics JSON.

The supplier's calculations and full report stay unchanged. Screen responses replace
``coverage.intervals`` with ``coverage.intervals_ref`` pointing into ``interval_sets``.
Every observation, Decimal string, coverage count and caveat is retained. The database
stores this form too; full-detail callers get the original shape back on read.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def compact_report(report: dict) -> dict:
    if report.get("interval_sets"):
        return report
    samples: dict[str, list] = {}
    seen: dict[int, str] = {}

    def visit(node: Any) -> Any:
        if isinstance(node, list):
            return [visit(item) for item in node]
        if not isinstance(node, dict):
            return node
        out = {}
        for key, value in node.items():
            if key == "coverage" and isinstance(value, dict) and "intervals" in value:
                intervals = value["intervals"]
                ref = seen.get(id(intervals))
                if ref is None:
                    encoded = json.dumps(intervals, separators=(",", ":")).encode()
                    ref = hashlib.sha256(encoded).hexdigest()
                    seen[id(intervals)] = ref
                    samples.setdefault(ref, intervals)
                out[key] = {k: visit(v) for k, v in value.items() if k != "intervals"}
                out[key]["intervals_ref"] = ref
            else:
                out[key] = visit(value)
        return out

    compact = visit({k: v for k, v in report.items() if k != "interval_sets"})
    compact["interval_sets"] = samples
    return compact


def expand_report(report: dict) -> dict:
    if "interval_sets" not in report:
        return report
    samples = report["interval_sets"]

    def visit(node: Any) -> Any:
        if isinstance(node, list):
            return [visit(item) for item in node]
        if not isinstance(node, dict):
            return node
        out = {k: visit(v) for k, v in node.items() if k != "intervals_ref"}
        if "intervals_ref" in node:
            out["intervals"] = samples[node["intervals_ref"]]
        return out

    return visit({k: v for k, v in report.items() if k != "interval_sets"})
