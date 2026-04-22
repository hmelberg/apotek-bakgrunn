"""HTTP endpoints exposed by the apotek Q&A Anvil app.

All endpoints are reachable at:
    https://<app>.anvil.app/_/api<path>

All endpoints except /health require a valid X-API-Key header.

Endpoints:
    POST /ask            — fritekst-spørsmål → {answer, citations,
                           has_direct_coverage, suggested_followups, ...}
    GET  /search         — semantic-retrieval-only (debug / search box)
    GET  /examples       — liste med forhåndsdefinerte eksempelspørsmål
    GET  /facts          — liste med kurerte datasett for figurer
    GET  /facts/<id>     — ett datasett
    GET  /health         — liveness (ingen auth)
"""

from __future__ import annotations

import json
import time

import anvil.server
from anvil.server import HttpResponse

import examples
import facts
import generation
import retrieval
import utils


def _json(body, status: int = 200) -> HttpResponse:
    return HttpResponse(
        status=status,
        body=json.dumps(body, ensure_ascii=False),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )


def _load_body() -> dict:
    req = anvil.server.request
    try:
        body = req.body_json
    except Exception:
        body = None
    if body is None and req.body:
        try:
            raw = req.body.get_bytes()
        except Exception:
            raw = b""
        for enc in ("utf-8", "cp1252", "latin-1"):
            try:
                body = json.loads(raw.decode(enc))
                break
            except Exception:
                continue
    return body or {}


def _authenticate_or_fail():
    req = anvil.server.request
    alias = utils.authenticate(req)
    if not alias:
        return None, _json({"error": "invalid or missing X-API-Key"}, status=401)
    if not utils.check_rate_limit(alias):
        return None, _json({"error": "rate limit exceeded"}, status=429)
    return alias, None


# ---------------------------------------------------------------------------
# /ask


@anvil.server.http_endpoint("/ask", methods=["POST"], cross_site_session=False, enable_cors=True)
def http_ask():
    alias, err = _authenticate_or_fail()
    if err:
        return err

    body = _load_body()
    question = (body.get("question") or "").strip()
    if not question:
        return _json({"error": "missing 'question'"}, status=400)
    try:
        k = int(body.get("k", 12))
    except (TypeError, ValueError):
        k = 12
    k = max(4, min(k, 24))

    source_ids = body.get("source_ids")
    if source_ids is not None and not isinstance(source_ids, list):
        return _json({"error": "'source_ids' must be a list of strings"}, status=400)

    t0 = time.time()
    try:
        result = generation.answer_question(
            question=question, k=k, source_ids=source_ids
        )
    except Exception as exc:
        latency_ms = int((time.time() - t0) * 1000)
        utils.log_request(
            endpoint="/ask",
            question=question,
            latency_ms=latency_ms,
            api_key_alias=alias,
            error=f"{type(exc).__name__}: {exc}",
        )
        return _json({"error": "internal error", "detail": str(exc)}, status=500)
    latency_ms = int((time.time() - t0) * 1000)

    utils.log_request(
        endpoint="/ask",
        question=question,
        model=result.get("model", ""),
        answer=result.get("answer", ""),
        citations=result.get("citations", []),
        latency_ms=latency_ms,
        cache_stats=result.get("cache_stats") or {},
        api_key_alias=alias,
    )
    result["latency_ms"] = latency_ms
    return _json(result)


# ---------------------------------------------------------------------------
# /search  (retrieval only; useful for debug + a search-as-you-type UI)


@anvil.server.http_endpoint("/search", methods=["GET"], cross_site_session=False, enable_cors=True)
def http_search(**kwargs):
    alias, err = _authenticate_or_fail()
    if err:
        return err
    q = (kwargs.get("q") or "").strip()
    if not q:
        return _json({"error": "missing 'q'"}, status=400)
    try:
        k = int(kwargs.get("k", 12))
    except (TypeError, ValueError):
        k = 12
    k = max(1, min(k, 25))

    source_filter = (kwargs.get("source_id") or "").strip() or None
    source_ids = [source_filter] if source_filter else None

    hits = retrieval.server_search(query=q, k=k, source_ids=source_ids)
    return _json({"results": hits})


# ---------------------------------------------------------------------------
# /examples


@anvil.server.http_endpoint("/examples", methods=["GET"], cross_site_session=False, enable_cors=True)
def http_examples():
    alias, err = _authenticate_or_fail()
    if err:
        return err
    return _json({"groups": examples.all_groups()})


# ---------------------------------------------------------------------------
# /facts


@anvil.server.http_endpoint("/facts", methods=["GET"], cross_site_session=False, enable_cors=True)
def http_facts():
    alias, err = _authenticate_or_fail()
    if err:
        return err
    return _json({"facts": facts.all_facts()})


@anvil.server.http_endpoint("/facts/:fid", methods=["GET"], cross_site_session=False, enable_cors=True)
def http_fact(fid: str, **_kwargs):
    alias, err = _authenticate_or_fail()
    if err:
        return err
    fact = facts.get_fact(fid)
    if fact is None:
        return _json({"error": f"unknown fact id '{fid}'"}, status=404)
    return _json(fact)


# ---------------------------------------------------------------------------
# /health  (no auth)


@anvil.server.http_endpoint("/health", methods=["GET"], cross_site_session=False, enable_cors=True)
def http_health():
    try:
        stats = retrieval.corpus_stats()
        return _json({
            "status": "ok",
            "chunks": stats.get("chunks", 0),
            "sources": stats.get("sources", 0),
        })
    except Exception as exc:
        return _json({"status": "degraded", "error": str(exc)}, status=503)
