"""Claude call with prompt caching — apotek-API Q&A.

Single entry point:
    answer_question(question, k=12, source_ids=None) -> dict

One-shot: model gets the top-k retrieved chunks up front and answers in
one Claude call. No tool-use loops.
"""

from __future__ import annotations

import json
import re

import anvil.secrets
from anthropic import Anthropic

import prompts
import retrieval

DEFAULT_MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 900


def _client() -> Anthropic:
    api_key = anvil.secrets.get_secret("ANTHROPIC_API_KEY")
    return Anthropic(api_key=api_key)


def _cached_prefix_block() -> dict:
    return {
        "type": "text",
        "text": prompts.cached_prefix(),
        "cache_control": {"type": "ephemeral"},
    }


_JSON_OBJ_RE = re.compile(r"\{(?:[^{}]|(?:\{[^{}]*\}))*\}", re.DOTALL)


def _parse_json_response(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        first_nl = text.find("\n")
        if first_nl > 0:
            text = text[first_nl + 1:]
        if text.endswith("```"):
            text = text[:-3]
    try:
        return json.loads(text)
    except Exception:
        return None


def _recover_partial_json(raw: str) -> dict | None:
    if not raw:
        return None
    candidates = _JSON_OBJ_RE.findall(raw)
    if not candidates:
        return None
    candidates.sort(key=len, reverse=True)
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None


def _resolve_citations(raw_citations: list, retrieved: list[dict]) -> list[dict]:
    """Map the model's `{n, note}` entries to full citation records with
    document + page + snippet pulled from the retrieval result. Drops any
    `n` out of range.
    """
    out: list[dict] = []
    seen: set[int] = set()
    for c in raw_citations or []:
        if not isinstance(c, dict):
            continue
        try:
            n = int(c.get("n"))
        except (TypeError, ValueError):
            continue
        if n in seen or n < 1 or n > len(retrieved):
            continue
        seen.add(n)
        chunk = retrieved[n - 1]
        snippet = (chunk.get("text") or "")
        if len(snippet) > 320:
            snippet = snippet[:320].rstrip() + " …"
        rec = {
            "n": n,
            "document": chunk.get("document", ""),
            "title": chunk.get("title", ""),
            "source_id": chunk.get("source_id", ""),
            "page": chunk.get("page"),
            "section": chunk.get("section"),
            "snippet": snippet,
        }
        note = (c.get("note") or "").strip()
        if note:
            rec["note"] = note
        out.append(rec)
    out.sort(key=lambda r: r["n"])
    return out


def answer_question(
    question: str,
    k: int = 12,
    source_ids: list[str] | None = None,
) -> dict:
    """Answer a question and return:

        {
          "answer": str,
          "citations": list[{n, document, page, ...}],
          "has_direct_coverage": bool,
          "suggested_followups": list[str],
          "model": str,
          "cache_stats": dict,
        }
    """
    retrieved = retrieval.search(question, k=k, source_ids=source_ids)

    if not retrieved:
        return {
            "answer": (
                "Kildene jeg har tilgjengelig dekker ikke dette spørsmålet. "
                "Prøv å omformulere, eller still et mer spesifikt spørsmål "
                "om norsk apotek, legemiddelbruk eller fastlegeerfaringer."
            ),
            "citations": [],
            "has_direct_coverage": False,
            "suggested_followups": [
                "Hvilke temaer dekkes i kildene dine?",
                "Hva sier rapportene om pasienttilfredshet med fastlegen?",
                "Finnes det tall på polyfarmasi blant eldre i kildene?",
            ],
            "model": "",
            "cache_stats": {},
        }

    dynamic = prompts.render_retrieved_chunks(retrieved)
    messages = [
        {
            "role": "user",
            "content": [
                _cached_prefix_block(),
                {
                    "type": "text",
                    "text": (
                        f"# Brukerens spørsmål\n\n{question}\n\n"
                        f"{dynamic}\n\n{prompts.ASK_OUTPUT_CONTRACT}"
                    ),
                },
            ],
        }
    ]

    client = _client()
    resp = client.messages.create(
        model=DEFAULT_MODEL,
        max_tokens=MAX_TOKENS,
        system=prompts.SYSTEM_PROMPT,
        messages=messages,
    )

    text_out = ""
    for block in resp.content:
        if getattr(block, "type", "") == "text":
            text_out = block.text
            break

    parsed = _parse_json_response(text_out) or _recover_partial_json(text_out)
    usage = resp.usage.model_dump() if hasattr(resp.usage, "model_dump") else dict(resp.usage)

    if parsed is None:
        # Falls back to raw text so the user gets something rather than an error.
        return {
            "answer": text_out or "",
            "citations": [],
            "has_direct_coverage": False,
            "suggested_followups": [],
            "model": DEFAULT_MODEL,
            "cache_stats": usage,
        }

    citations = _resolve_citations(parsed.get("citations") or [], retrieved)
    followups = parsed.get("suggested_followups") or []
    # Sanity: keep only non-empty strings, up to 3.
    followups = [
        s.strip() for s in followups
        if isinstance(s, str) and s.strip()
    ][:3]

    return {
        "answer": (parsed.get("answer") or "").strip(),
        "citations": citations,
        "has_direct_coverage": bool(parsed.get("has_direct_coverage")),
        "suggested_followups": followups,
        "model": DEFAULT_MODEL,
        "cache_stats": usage,
    }
