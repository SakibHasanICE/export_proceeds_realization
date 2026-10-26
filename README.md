# Export Proceeds Realization automation

Bank mail (IMAP) -> PDF -> rule-based parser (LLM fallback) -> database -> review UI -> ERP form payload.

## Run locally
1. `pip install -r requirements.txt`
2. `cp .env.example .env` and fill in IMAP_*, KNOWN_BANK_SENDERS, OPENROUTER_API_KEY, DATABASE_URL
3. Fill `app/config/account_map.json` (account head + AC/Loan No per account)
4. `python -m app.main`  -> API http://localhost:8000/docs, UI http://localhost:8501

## Docker
`docker compose up --build` (Postgres + app). Set API_KEY in .env when binding 0.0.0.0.

## Tests
`python -m pytest -q`

## Add a new bank / layout
Write `parse_<bank>()` in `app/components/bank_parsers.py`, register it in `PARSERS`, add a test.
