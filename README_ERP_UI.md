# Export Proceeds Realization: Next.js front end + tracked ERP robot

## What changed
| Path | What |
|---|---|
| `frontend/` | NEW Next.js 14 app (replaces Streamlit): document table, per-row **Proceed to ERP**, **Proceed all to ERP**, live run tracker |
| `app/erp_worker/erp_data.py` | Refactored robot: reports every step, structured errors, retries, verifies every value. **Never clicks Save.** |
| `app/erp_worker/runner.py` | NEW job queue (one run at a time, history saved in `/data/erp_runs`) |
| `app/erp_worker/view.py` | NEW: resolves account heads and builds what the table shows / what the robot types |
| `app/backend/erp_api.py` + `api.py` | NEW `/erp/...` endpoints (entries, runs, batches, screenshots) |
| `app/config/account_map.json` | `…146005` and `…60009` -> `BTB Margin/DFC/BLO/DAD/RAD/FBPAR A/C`; `…146002` -> `ERQ Account` |
| `app/main.py` | No longer starts Streamlit |
| `docker-compose.yml` | New `web` service on port 3000 (`WEB_PORT` in `.env`) |

## Run it
```bash
docker compose build app web
docker compose up -d
```
Open http://localhost:3000

Existing entries keep an empty Account Head in the database; the table and the robot fill it from
`account_map.json` by AC / Loan No (the table marks those with a small "map" tag).

## Safety
The robot fills the form, reads every box back, takes a full-page screenshot and closes the browser.
There is no Save click anywhere in the code.
