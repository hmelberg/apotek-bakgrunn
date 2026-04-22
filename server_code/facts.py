"""Serve curated numerical datasets from data/facts.json.

The file is uploaded to Anvil Data Files at deploy time. We load it on
first access and cache; `reload_facts()` re-reads after a re-upload.

Each fact is:
    {
      "id": "unique-key",
      "title": "Human-readable title",
      "source": {"document": "...pdf", "page": 14},
      "chart_type": "bar" | "line" | "stacked-bar",
      "x_label": "...",
      "y_label": "...",
      "series": [
        {"name": "Label", "points": [["x1", v1], ["x2", v2], ...]}
      ]
    }
"""

from __future__ import annotations

import json

import anvil.server
from anvil.files import data_files


_facts: dict[str, dict] | None = None


def _load() -> None:
    global _facts
    try:
        with open(data_files["facts.json"], "r", encoding="utf-8") as f:
            rows = json.load(f)
    except Exception:
        rows = []
    out: dict[str, dict] = {}
    for r in rows:
        if isinstance(r, dict) and r.get("id"):
            out[r["id"]] = r
    _facts = out


def _ensure_loaded() -> None:
    if _facts is None:
        _load()


@anvil.server.callable
def reload_facts() -> dict:
    _load()
    return {"facts": len(_facts or {})}


def all_facts() -> list[dict]:
    _ensure_loaded()
    return list((_facts or {}).values())


def get_fact(fid: str) -> dict | None:
    _ensure_loaded()
    return (_facts or {}).get(fid)
