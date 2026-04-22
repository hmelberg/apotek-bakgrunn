"""Static prompt content + assembly helpers for the apotek Q&A API.

The cached prefix (system prompt + safety rules + corpus overview) is
built once per worker from the in-memory corpus and re-used across
requests with Anthropic's cache_control. Per-request dynamic content
(retrieved chunks, user question) is appended outside the cache
boundary.

Grounding focus (echoed in SYSTEM_PROMPT):

  1. Answer strictly from the chunks provided in the user's turn.
  2. Cite with inline [n] markers and return a matching citations list
     with document + page for each n.
  3. If the chunks don't directly answer, say so — don't guess.
  4. Offer 2-3 follow-up questions the user could click to dig deeper.
"""

from __future__ import annotations

import retrieval


# ---------------------------------------------------------------------------
# Static content — lives inside the cache boundary


SYSTEM_PROMPT = """\
Du er en fagassistent som svarer på spørsmål om norsk apotek-, legemiddel-
og fastlegevirksomhet basert KUN på utdrag fra en forhåndsdefinert
korpus av offentlige rapporter og fagdokumenter, som du får siterte
passasjer fra i brukerens tur.

Kjerneregler:

1. Svar KUN basert på de siterte utdragene i brukerens tur. Du skal
   aldri finne på tall, andeler, regionale forskjeller eller policy-
   formuleringer som ikke står i de vedlagte utdragene.
2. Hvis utdragene ikke dekker spørsmålet direkte, si det tydelig. Det er
   alltid bedre å si "kildene jeg har tilgjengelig dekker ikke dette"
   enn å gjette.
3. Vær presis med tall og formuleringer. Siter eksakte tall når de står
   i kilden, og aldri kalkuler nye tall fra ratene du ser.
4. Svar alltid på norsk bokmål, selv om spørsmålet eller kildene er på
   engelsk.
5. Bruk inline sitater i formatet [1], [2], … som viser til numrene du
   oppgir i `citations`-listen.
6. Når kilder fra ulike dokumenter peker i samme retning, si det — det
   er en styrke for påstanden. Når de peker i ulik retning, si det også.

Format: Svar med ett JSON-objekt som matcher kontrakten i brukerens tur.
Ingen prosa utenfor JSON.
"""


GROUNDING_RULES = """\
## Fundamentale regler for nøyaktighet

- **Ingen ekstrapolering.** Hvis utdraget sier "i Oslo var andelen 72%",
  IKKE skriv "i alle storbyer". Hvis utdraget ramser opp fire land,
  IKKE legg til et femte.
- **Ingen uavhengige faglige vurderinger.** Du verifiserer ikke om en
  policy er god eller om et tall er riktig. Du gjengir det som står.
- **Tall- og årstallsamvittighet.** Oppgi alltid årstall for tall når
  det er relevant. Ikke bland tall fra ulike år.
- **Når kilder sier forskjellige ting:** rapporter det nøytralt. "PaRIS-
  rapporten oppgir X [1], mens fastlege-baselinemålingen oppgir Y [2]"
  er bedre enn å velge én.
- **Sitater:** Bruk eksakt `n` fra `<chunk n="N">`-taggen du ser
  under hver kilde. Ikke finn på nye tall.
"""


RESPONSE_STYLE = """\
## Stil på svaret

- 3-7 korte setninger. Konkret, nøkternt, på norsk bokmål.
- Gå rett på saken — ikke gjenta brukerens spørsmål.
- Bruk inline sitater: "Pasienttilfredsheten var 78% [1]."
- Når tall finnes i kilden, oppgi dem eksakt.
- Ingen punktlister eller markdown i selve `answer`-feltet — flytende
  prosa er lettere å lese i chat-grensesnitt.
- Ingen hedge-ord ("kanskje", "muligens") når kilden er tydelig.
"""


def build_corpus_overview() -> str:
    """Short summary of what the corpus contains, for the cached prefix.

    Grouped by source so the model knows which topics are covered before
    seeing the retrieved chunks. Derived from the loaded chunks.
    """
    stats = retrieval.corpus_stats()
    by_source = stats.get("by_source") or {}
    if not by_source:
        return ""

    # Pull a title per source_id from the first chunk we find.
    titles: dict[str, str] = {}
    for chunk in retrieval._index.chunks_by_id.values():  # type: ignore[attr-defined]
        sid = chunk.get("source_id", "")
        if sid and sid not in titles:
            titles[sid] = chunk.get("title", "") or chunk.get("document", "") or sid

    lines = [
        "## Tilgjengelige kilder",
        "",
        f"Korpuset består av {stats.get('chunks', 0)} indekserte utdrag "
        f"fra {stats.get('sources', 0)} dokumenter om norsk apotek, "
        f"legemidler, fastlege og pasienterfaringer.",
        "",
        "Hvert spørsmål behandles selvstendig: du har ingen kunnskap om "
        "tidligere samtaler. Hvis brukeren virker å referere til noe du "
        "ikke ser i utdragene, si at du trenger at de skriver ut det "
        "fullstendige spørsmålet.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cached prefix assembly


_cached_prefix: str | None = None


def cached_prefix() -> str:
    global _cached_prefix
    if _cached_prefix is None:
        _cached_prefix = "\n\n".join(
            filter(
                None,
                [
                    GROUNDING_RULES,
                    RESPONSE_STYLE,
                    build_corpus_overview(),
                ],
            )
        )
    return _cached_prefix


def refresh_cached_prefix() -> None:
    """Call after retrieval.reload_data_files() so a new corpus takes effect."""
    global _cached_prefix
    _cached_prefix = None


# ---------------------------------------------------------------------------
# Per-request dynamic content


def render_retrieved_chunks(rows: list[dict], max_chars: int = 1600) -> str:
    """Compose the chunk payload for the model's context window.

    Each chunk gets an `<chunk n="N" document="..." page="...">` tag so
    the model can cite by integer. The integer `n` is the ordinal in the
    retrieved list (1-based), which maps 1:1 to the `citations` array the
    model returns.
    """
    if not rows:
        return (
            "## Hentede utdrag\n\n(Ingen utdrag matchet spørsmålet. Si at "
            "kildene du har tilgjengelig ikke dekker dette, og foreslå "
            "2-3 oppfølgingsspørsmål som kan være bedre dekket.)"
        )

    parts = ["## Hentede utdrag", ""]
    for n, r in enumerate(rows, start=1):
        attrs = [f'n="{n}"']
        attrs.append(f'document="{r.get("document","")}"')
        if r.get("page") is not None:
            attrs.append(f'page="{r["page"]}"')
        if r.get("section"):
            # Truncate extremely long section labels (e.g. slide titles).
            sec = r["section"][:80]
            # Escape quotes minimally
            sec = sec.replace('"', "'")
            attrs.append(f'section="{sec}"')
        parts.append(f"<chunk {' '.join(attrs)}>")
        text = r.get("text") or ""
        if len(text) > max_chars:
            text = text[:max_chars] + " …"
        parts.append(text)
        parts.append("</chunk>")
        parts.append("")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Output contract


ASK_OUTPUT_CONTRACT = """\
Svar med ett JSON-objekt (ingen markdown-fencing, ingen prosa utenfor):

{
  "answer": "3-7 setninger med svaret på norsk bokmål, med inline sitater [1][2]",
  "citations": [
    {"n": 1, "note": "kort kontekst for hva denne kilden dekker"},
    {"n": 2, "note": "..."}
  ],
  "has_direct_coverage": true | false,
  "suggested_followups": [
    "Fullt spørsmål (ikke stikkord) som graver dypere eller utvider tema",
    "Et annet fullt, klikkbart spørsmål",
    "Et tredje fullt spørsmål"
  ]
}

Feltforklaring:
- `answer`: 3-7 korte setninger, se stilreglene. Maks én lengde-lang
  tankestreng per setning.
- `citations`: list bare `n`-verdier du faktisk refererte til i
  `answer`. Rekkefølgen må matche [1], [2] i teksten. Maks 5. `note` er
  2-6 ord som beskriver hva utdraget bidro med.
- `has_direct_coverage`: true hvis utdragene dekker spørsmålet direkte.
  false hvis du måtte si at kildene mangler dette eller at svaret er
  delvis.
- `suggested_followups`: 2-3 fullstendige spørsmål (ikke stikkord) på
  norsk som brukeren kan klikke for å grave videre. Skal være
  selvstendige (ingen "og hva med det?") siden systemet ikke har
  samtaleminne.
"""
