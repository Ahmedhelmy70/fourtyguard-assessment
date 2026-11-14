# FortyGuard API Tester

A small Python script that exercises the [FortyGuard Temperature API](https://docs-api.fortyguard.com) and produces a report of what the API does with valid, invalid and unexpected requests. It was written to help answer an API review with questions like:

1. Did you identify any API design issues or missing endpoints?
2. Link to supporting files, reports, screenshots or presentations.
3. How would you improve the API documentation?

The script does not decide the answers for you. It collects evidence (status codes, error bodies, timings, headers) and flags candidate findings. Check each one against the official docs before you report it.

## What it tests

| Group | What it checks |
|---|---|
| `auth` | Missing and invalid `api-key` are rejected with 401/403 |
| `validation` | Bad payloads are rejected with 400/422: `date_time` as a string, missing fields, invalid `filter_type`, bad dates and times, bad `granularity` and `analytic_type`, unclosed polygon ring, non-GeoJSON, non-JSON body |
| `status` | `GET /v1/status/{id}` with an unknown or malformed id |
| `methods` | `GET` on the `POST`-only `/v1/heatmap` |
| `discovery` | Probes common endpoints (list jobs, cancel job, credits, usage, account, health, OpenAPI spec) to see which exist |
| `live` | Optional full round trip: submit, poll until done, save the result, resubmit the identical request to see caching behaviour |
| `errors` / `headers` | Whether all 4xx responses share one schema, and whether rate-limit headers are exposed |

## Requirements

- Python 3.9+
- A FortyGuard API key
- `requests` (see `requirements.txt`)

## Setup

```bash
git clone https://github.com/Ahmedhelmy70/fourtyguard-assessment
cd fourtyguard-assessment

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export FORTYGUARD_API_KEY="your-api-key"
```

Never commit your key. `.env` and `reports/` are already in `.gitignore`.

## Usage

```bash
# Auth, validation, status and endpoint-discovery checks (no job should be created)
python main.py

# Also run a real submit -> poll -> resubmit round trip (consumes credits)
python main.py --live

# Also run riskier cases: oversized polygon, area outside US coverage
python main.py --extended

# Useful options
python main.py --live --date 2026-11-01 --granularity 60 --analytic-type tcm --timeout 600
```

| Option | Default | Description |
|---|---|---|
| `--live` | off | Run the full round trip (uses credits) |
| `--extended` | off | Run riskier validation cases |
| `--date` | 7 days ago | Query date, `YYYY-MM-DD`. Very recent data may not be ingested yet |
| `--granularity` | `100` | Tile edge in metres: `60`, `80` or `100` |
| `--analytic-type` | none | `tcm`, `time_of_measure`, `exceedance` or `persistence` |
| `--timeout` | `300` | Max seconds to wait for a job |
| `--out-dir` | `reports` | Where reports and downloaded results go |

Set `FORTYGUARD_BASE_URL` to point the script at a different host (default `https://api.fortyguard.com`).

The exit code is `0` if no check failed, `1` if any failed or errored, and `2` if the API key is missing.

### Credit warning

If the API wrongly accepts an invalid request, it may start a job and use credits. The validation cases are intended to be rejected, but that is exactly what the script is testing. The `--extended` cases are the most likely to be accepted, so they are off by default.

## Output

Each run writes two files to `reports/`:

- `report_<timestamp>.md`: a readable table of every check, plus candidate findings grouped by review question
- `report_<timestamp>.json`: the same data in machine-readable form

With `--live`, the downloaded heatmap result is saved as `result_<activity_id>.json`.

Example console output:

```
[PASS ] auth       auth_invalid_key                   HTTP 401  error keys=['error', 'message']; ...
[PASS ] validation date_time_is_string                HTTP 422  error keys=['detail', 'error', ...]
[INFO ] discovery  probe GET /v1/credits              HTTP 404  credits/balance: ...
[PASS ] live       live_poll                          HTTP -    final=completed; statuses seen=[...]
```

Outcomes: `PASS` (behaved as expected), `FAIL` (did not), `INFO` (informational), `ERROR` (network problem), `SKIP` (not run).

## Request format

The heatmap request is asynchronous. `POST /v1/heatmap` returns an `activity_id`, and you poll `GET /v1/status/{activity_id}` until the job finishes. Authentication is the `api-key` header.

```json
{
  "polygon_aoi": {
    "type": "FeatureCollection",
    "features": [
      {
        "type": "Feature",
        "properties": {},
        "geometry": {
          "type": "Polygon",
          "coordinates": [[[-74.0170, 40.7050], [-74.0170, 40.7100], [-74.0110, 40.7100], [-74.0110, 40.7050], [-74.0170, 40.7050]]]
        }
      }
    ]
  },
  "date_time": { "filter_type": 3, "start_date": "2026-11-07" },
  "granularity": 100
}
```

`date_time` must be an object, not a string. Sending `"date_time": "2026-11-14T12:00:00Z"` returns HTTP 422 (`Input should be a valid dictionary or object`).

`filter_type` values (as described in public integrations of the API; confirm in the official docs):

| `filter_type` | Meaning | Fields used |
|---|---|---|
| 1 | Single hour | `start_date`, `start_time` |
| 2 | Range of hours, same day | `start_date`, `start_time`, `end_time` |
| 3 | Single day | `start_date` |
| 4 | Range of days | `start_date`, `end_date` |

Other details reported by public integrations, which this script's report can help you confirm or refute:

- `start_time` is interpreted as local time at the area of interest, not UTC.
- Documented coverage is the United States.
- Status values appear capitalised (for example `Processing`, `Completed`), so compare them case-insensitively.
- Very recent dates may have no data yet because of ingestion lag.

## Using the report for the review form

| Form question | Where to look |
|---|---|
| Design issues or missing endpoints | Report section "Q1", failed validation checks, `discovery` probes, `error_shape_consistency`, `rate_limit_headers` |
| Supporting files or links | Link this repository and attach a generated `report_*.md` |
| Documentation improvements | Report section "Q3", plus anything where the real behaviour differed from what the docs say |

Treat a `404` on a discovery probe as "I could not find this endpoint", not proof that it does not exist. The path may simply differ from the one guessed here.

## Project layout

```
fortyguard-api-tester/
├── main.py            # test runner and report generator
├── requirements.txt
├── README.md
├── .gitignore
└── reports/           # generated, git-ignored
```

## Extending

Add a new validation case by appending a tuple to the `cases` list in `Runner.check_validation`: `(name, description, mutator, extended_only)`. The mutator receives a valid payload and returns a modified copy.

## Disclaimer

This is an independent testing tool, not an official FortyGuard project. Use it only with your own API key and within your plan's limits and the provider's terms.
