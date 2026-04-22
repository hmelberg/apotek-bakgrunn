"""Embedding-based retrieval for the apotek Q&A API.

Corpus shape (all uploaded to Anvil Data Files):

    chunks.jsonl         — one JSON per line, shape:
                           {id, source_id, document, page, section,
                            text, kind, title}
    embeddings.npy       — float32 (N, 1024), L2-normalised, same row order
                           as chunks.jsonl
    chunk_ids.json       — ["id_0001", "id_0002", ...] row-aligned

At server-module import the three files are loaded once and kept in
module state. Cosine similarity is computed as a plain dot product
against the normalised matrix (numpy), so search is sub-millisecond for
N in the low thousands — no FAISS/Chroma needed.

Call `reload_data_files()` from the Server console after re-uploading any
of the files to pick up changes without restarting the worker.

The query-time embedding call is made to Voyage (`voyage-3`,
input_type="query") via the VOYAGE_API_KEY secret.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field

import anvil.secrets
import anvil.server
import numpy as np
from anvil.files import data_files


MODEL = "voyage-3"
DIMS = 1024


# ---------------------------------------------------------------------------
# Module-level state


@dataclass
class _Index:
    matrix: np.ndarray | None = None     # (N, DIMS), L2-normalised
    ids: list[str] = field(default_factory=list)
    chunks_by_id: dict[str, dict] = field(default_factory=dict)


_index = _Index()
_voyage_client = None  # lazy


def _load_voyage_client():
    global _voyage_client
    if _voyage_client is None:
        import voyageai
        api_key = anvil.secrets.get_secret("VOYAGE_API_KEY")
        _voyage_client = voyageai.Client(api_key=api_key)
    return _voyage_client


def _load_data_files() -> None:
    global _index

    # chunks.jsonl
    chunks_by_id: dict[str, dict] = {}
    with open(data_files["chunks.jsonl"], "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                row = json.loads(line)
                chunks_by_id[row["id"]] = row

    # embeddings.npy — load from Anvil Data File via BytesIO since np.load
    # wants a seekable buffer.
    with open(data_files["embeddings.npy"], "rb") as f:
        raw = f.read()
    matrix = np.load(io.BytesIO(raw))
    matrix = np.asarray(matrix, dtype=np.float32)

    # chunk_ids.json
    with open(data_files["chunk_ids.json"], "r", encoding="utf-8") as f:
        ids = json.load(f)

    if matrix.shape[0] != len(ids):
        raise RuntimeError(
            f"row mismatch: embeddings has {matrix.shape[0]} rows, "
            f"chunk_ids has {len(ids)}"
        )

    # Ensure the matrix is L2-normalised in case the build script was run
    # without normalisation. Cheap idempotent guard.
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    # avoid div-by-zero on degenerate rows
    norms[norms == 0] = 1.0
    if not np.allclose(norms, 1.0, atol=1e-3):
        matrix = matrix / norms

    _index = _Index(matrix=matrix, ids=ids, chunks_by_id=chunks_by_id)


def _ensure_loaded() -> None:
    if _index.matrix is None:
        _load_data_files()


@anvil.server.callable
def reload_data_files() -> dict:
    """Re-read chunks.jsonl + embeddings.npy + chunk_ids.json from Data Files.

    Also busts the cached prompt prefix so any new content takes effect.
    """
    _load_data_files()
    try:
        import prompts
        prompts.refresh_cached_prefix()
    except Exception:
        pass
    n = _index.matrix.shape[0] if _index.matrix is not None else 0
    by_source: dict[str, int] = {}
    for c in _index.chunks_by_id.values():
        sid = c.get("source_id", "")
        by_source[sid] = by_source.get(sid, 0) + 1
    return {
        "chunks": n,
        "sources": len(by_source),
        "by_source": by_source,
        "dim": _index.matrix.shape[1] if _index.matrix is not None else 0,
    }


# ---------------------------------------------------------------------------
# Accessors


def corpus_stats() -> dict:
    _ensure_loaded()
    n = _index.matrix.shape[0] if _index.matrix is not None else 0
    by_source: dict[str, int] = {}
    for c in _index.chunks_by_id.values():
        sid = c.get("source_id", "")
        by_source[sid] = by_source.get(sid, 0) + 1
    return {"chunks": n, "sources": len(by_source), "by_source": by_source}


def chunk_by_id(cid: str) -> dict | None:
    _ensure_loaded()
    return _index.chunks_by_id.get(cid)


# ---------------------------------------------------------------------------
# Query


def _embed_query(query: str) -> np.ndarray:
    client = _load_voyage_client()
    result = client.embed(texts=[query], model=MODEL, input_type="query")
    vec = np.asarray(result.embeddings[0], dtype=np.float32)
    n = np.linalg.norm(vec)
    if n > 0:
        vec = vec / n
    return vec


def search(query: str, k: int = 12, source_ids: list[str] | None = None) -> list[dict]:
    """Return the top-k chunks most similar to `query`.

    Each result is the chunk dict plus a float `score` (cosine similarity).
    If `source_ids` is given, results are restricted to those sources.
    """
    _ensure_loaded()
    if _index.matrix is None or not _index.ids:
        return []
    q_vec = _embed_query(query)
    scores = _index.matrix @ q_vec  # (N,) cosine similarity (matrix is normalised)
    # Wider candidate pool when filtering so k results survive.
    fetch = k * 3 if source_ids else k
    # argpartition + argsort is faster than full sort for large N, but at
    # N~few-thousand plain argsort is fine and avoids a subtle edge case.
    order = np.argsort(-scores)

    out: list[dict] = []
    src_filter = set(source_ids) if source_ids else None
    for idx in order:
        if len(out) >= k:
            break
        cid = _index.ids[int(idx)]
        chunk = _index.chunks_by_id.get(cid)
        if chunk is None:
            continue
        if src_filter and chunk.get("source_id") not in src_filter:
            continue
        row = dict(chunk)  # shallow copy; don't mutate cache
        row["score"] = float(scores[int(idx)])
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# Server-callable variant (used by /search debug endpoint and the UI)


@anvil.server.callable
def server_search(
    query: str,
    k: int = 12,
    source_ids: list[str] | None = None,
) -> list[dict]:
    hits = search(query=query, k=k, source_ids=source_ids)
    # Drop the long `text` field in search-only responses to keep payload
    # small; callers that need full text go through /ask.
    return [
        {
            "id": h["id"],
            "source_id": h.get("source_id", ""),
            "document": h.get("document", ""),
            "title": h.get("title", ""),
            "page": h.get("page"),
            "section": h.get("section"),
            "kind": h.get("kind", ""),
            "score": h.get("score", 0.0),
            "snippet": (h.get("text", "") or "")[:240],
        }
        for h in hits
    ]
