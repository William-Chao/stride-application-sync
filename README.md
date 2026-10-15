# Stride application sync

Local-first sync for workout apps. Speediance is the first supported app: it pulls one account's workout history, keeps the raw vendor payloads, and writes stable JSON (schema 1.3.0) for a separate reader or bot. Oura, Garmin, and Whoop can be added later as sibling connectors.

## Adding another app

Implement the connector contract in `scripts/connectors/base.py`: `source_name` plus `sync(output_dir) -> int`. A connector pulls one vendor, writes raw snapshots, then writes normalized JSON. It does not render a report.

## Layout

```
scripts/login.py                   email/password -> user id + token in .env
scripts/speediance_client.py       unofficial read-only API client
scripts/sync_speediance.py         raw monthly snapshots + normalized JSON
scripts/export_daily.py            one file per training day
scripts/run_pipeline.py            sync, then export
scripts/debug_training_detail.py   one-session probe of candidate detail endpoints
scripts/connectors/base.py         contract for future connectors
config/pr_exclusions.json          sets left out of PRs / est. 1RMs (empty by default)
schema/                            JSON Schema for every file a consumer reads
tests/fixtures/                    fake Speediance payloads
```

Output (`SPEEDIANCE_OUTPUT_DIR`, default `~/fitness-data`):

```
~/fitness-data/speediance/raw/monthly/YYYY-MM.json
~/fitness-data/speediance/normalized/history.json
~/fitness-data/speediance/normalized/by_exercise.json
~/fitness-data/status.json
```

Daily export (`SPEEDIANCE_DAILY_DIR`, default `~/fitness-data/daily`):

```
~/fitness-data/daily/YYYY-MM-DD.json
~/fitness-data/daily/latest.json
```

`~` is expanded to the user's home. Dates use America/Los_Angeles. Rest days are not written. Writes are atomic, and unchanged day files stay byte-identical.

## Setup

Python 3.11+.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

`.env` is gitignored. Never commit it or paste the token anywhere.

## Region

The account must already exist in the official Speediance app. `Global` (USA/Asia) uses `https://api2.speediance.com`; `EU` uses `https://euapi.speediance.com`. Tokens are bound to the host that issued them.

## Connect

### Option A — log in with email and password (recommended)

```bash
SPEEDIANCE_EMAIL=... SPEEDIANCE_PASSWORD=... SPEEDIANCE_REGION=Global \
  .venv/bin/python scripts/login.py
```

Pass credentials through the environment only (from a secret store, not typed into shell history if avoidable). The script calls `POST /api/app/v2/login/verifyIdentity`, then `POST /api/app/v2/login/byPass`, and writes `SPEEDIANCE_USER_ID`, `SPEEDIANCE_TOKEN`, `SPEEDIANCE_REGION`, and `SPEEDIANCE_UNIT` into `.env` (mode 600). It prints only `Connected.`, the region, and the unit. The email and password are never saved. Unset them afterward. Exit codes: 0 connected, 1 login rejected, 2 missing input, 3 network/server error.

The sync itself only ever uses the user id and token.

### Option B — paste a user id and token

If you already have them (from an unofficial manager app's Settings, or captured `App_user_id` / `Token` request headers), put them in `.env` as `SPEEDIANCE_USER_ID` and `SPEEDIANCE_TOKEN` and set `SPEEDIANCE_REGION` to match the server that issued the token.

`SPEEDIANCE_UNIT` is a fallback (`0` kg, `1` lb). The sync reads `weightUnit` from `GET /api/app/userinfo` and prefers it. Pounds are converted to kilograms for est. 1RMs; both units are stored.

## Run

```bash
.venv/bin/python scripts/run_pipeline.py
```

No-network smoke test:

```bash
SPEEDIANCE_FAKE=1 SPEEDIANCE_OUTPUT_DIR=/tmp/speediance-data \
SPEEDIANCE_DAILY_DIR=/tmp/speediance-daily .venv/bin/python scripts/run_pipeline.py
```

| Exit code | Meaning |
| --- | --- |
| 0 | Sync and export succeeded |
| 1 | Sync failed, export succeeded |
| 2 | Export failed |
| 3 | Sync and export both failed |

First sync pulls 12 months; later syncs start the month before the last success. Calls are paced (≥1.5 s). Rate limits back off 10/30/60 s, then the sync fails stale. HTTP 401 and API code 91 are not retried. A failed sync never overwrites good history.

To debug a session with a total load but no exercise rows:

```bash
.venv/bin/python scripts/debug_training_detail.py <training_id> --date YYYY-MM-DD
```

Output is redacted (no headers or secrets).

## Environment

| Variable | Default | Role |
| --- | --- | --- |
| `SPEEDIANCE_USER_ID` | empty | `App_user_id` header |
| `SPEEDIANCE_TOKEN` | empty | `Token` header |
| `SPEEDIANCE_REGION` | `Global` | `Global` or `EU` |
| `SPEEDIANCE_UNIT` | `0` | Fallback unit, `0` kg / `1` lb |
| `SPEEDIANCE_START_DATE` | unset | Force a start date |
| `SPEEDIANCE_REQUEST_DELAY_S` | `1.5` | Min seconds between calls |
| `SPEEDIANCE_OUTPUT_DIR` | `~/fitness-data` | Raw, normalized, status root |
| `SPEEDIANCE_DAILY_DIR` | `~/fitness-data/daily` | Per-day export |
| `SPEEDIANCE_FAKE` | unset | `1` replays `tests/fixtures/speediance_payload.json` |
| `SPEEDIANCE_FIXTURE` | that fixture | Override the fake payload |
| `SPEEDIANCE_NOW` | now | Freeze the clock |

`config/pr_exclusions.json` takes rules like `{"date": "YYYY-MM-DD", "exercise": "Name", "min_weight_kg": 100}` to drop bad sets from PRs.

## Schedule

No scheduler is installed. Example cron (8:30 PM local):

```cron
CRON_TZ=America/Los_Angeles
30 20 * * * cd ~/stride-application-sync && .venv/bin/python scripts/run_pipeline.py
```

## Tests

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest
```

## License and credits

Speediance has no public API for this data. Endpoint paths, header names, and the login flow follow MIT-licensed unofficial clients by hbui3 and forks; see `NOTICE.md` for the copyright notice. Unofficial API: it can change or break without notice.
