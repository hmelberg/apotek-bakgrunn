# Apotek Q&A — statisk nettside

Selvstendig `index.html` (ingen byggesteg, ingen avhengigheter utover Plotly fra CDN). Snakker mot Anvil HTTP-API-et (`https://apotek-bakgrunn.anvil.app/_/api`).

## Før du deployer: opprett en public API-nøkkel

Nettsiden sender en API-nøkkel i `X-API-Key`-headeren. Siden nøkkelen er synlig for alle som åpner siden, bruk en **egen public-nøkkel** som rate-limiteren holder under 30 kall/minutt.

I Anvil IDE → Secrets:

1. Opprett secret `API_KEY_PUBLIC` med verdi = en tilfeldig streng, f.eks.:
   ```
   python -c "import secrets; print(secrets.token_urlsafe(24))"
   ```
2. Åpne `API_KEY_ALIASES` og legg til `PUBLIC` (kommaseparert):
   ```
   HANS,PUBLIC
   ```
3. Trykk **Publish** i Anvil.

I `index.html`, finn linjen:
```js
const API_KEY = "REPLACE_WITH_PUBLIC_KEY";
```
og bytt `REPLACE_WITH_PUBLIC_KEY` med verdien du nettopp satte i `API_KEY_PUBLIC`.

## Lokal test

```bash
cd anvil_repo/website
python -m http.server 8000
# åpne http://localhost:8000
```

## Deploy på Netlify via GitHub

Siden denne mappen ligger inne i `anvil_repo/` som er synket til `https://github.com/hmelberg/apotek-bakgrunn`, er det enklest å la Netlify deploye derfra:

1. Logg inn på Netlify → **Add new site → Import an existing project**
2. Velg **GitHub** → autorisér → finn `hmelberg/apotek-bakgrunn`
3. **Build settings**:
   - Branch to deploy: `master`
   - Build command: *(tom)*
   - **Publish directory: `website`**
4. Deploy. Første deploy tar ~20 sekunder.

Ved hver push til `master` vil Netlify automatisk bygge og publisere ny versjon.

## Oppdatere nøkkel eller API-URL etter deploy

Endringer i `index.html` må commits til repoet og pushes før Netlify plukker dem opp. Det er *ingen* fare for å lekke hemmeligheter — `API_KEY` er public-nøkkelen; den kraftige Voyage/Anthropic-nøkkelen ligger kun i Anvil Secrets, ikke i koden.

## Hva nettsiden viser

**Tom tilstand (før bruker spør):**
- Søkeboks øverst
- Seksjon "Eksempelspørsmål" gruppert etter tema (fra `/examples`)
- Seksjon "Tall og figurer" med Plotly-plott + intro-tekst + 2-3 klikkbare spørsmål per figur (fra `/facts`)

**Etter spørsmål:**
- Spørsmålet gjengis
- Svaret med inline `[n]`-siteringer (klikk på et tall for å hoppe til kilden)
- Kilde-panel med dokument, sidetall, snippet
- "Grav videre" — klikkbare oppfølgingsspørsmål fra modellen
- Tilbake-lenke til startsiden

## Endre fakta-figurene

Hvert datasett i `/facts` kommer fra `data/facts.json` (bygget av `scripts/build_facts.py` og lastet opp til Anvil Data Files). For å legge til nye figurer eller fylle inn tall:

1. Rediger `scripts/build_facts.py` i apotek-prosjektet
2. `python scripts/build_facts.py`
3. Last opp ny `data/facts.json` til Anvil Data Files (overskriv)
4. I Anvil Server Console: `anvil.server.call('reload_facts')`

Siden cacher `/facts`-responsen én gang per load, så brukere får nyeste tall neste gang de åpner siden.
