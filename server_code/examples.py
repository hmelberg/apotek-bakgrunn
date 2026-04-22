"""Serve the predefined Norwegian example questions from data/examples.json.

Structure:
    [
      {
        "group": "Pasienttilfredshet",
        "questions": ["Hva sier PaRIS ...", "Hvordan ..."]
      },
      ...
    ]
"""

from __future__ import annotations

import json

import anvil.server
from anvil.files import data_files


_groups: list[dict] | None = None


def _load() -> None:
    global _groups
    try:
        with open(data_files["examples.json"], "r", encoding="utf-8") as f:
            _groups = json.load(f)
    except Exception:
        _groups = []


def _ensure_loaded() -> None:
    if _groups is None:
        _load()


@anvil.server.callable
def reload_examples() -> dict:
    _load()
    return {
        "groups": len(_groups or []),
        "total_questions": sum(len(g.get("questions") or []) for g in (_groups or [])),
    }


def all_groups() -> list[dict]:
    _ensure_loaded()
    return list(_groups or [])
