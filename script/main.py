#!/usr/bin/env python3
"""FortyGuard API test harness.

Exercises the FortyGuard Temperature API (https://docs-api.fortyguard.com) and
writes a report you can use to answer API-review questions such as:

  1. Did you identify any API design issues or missing endpoints?
  2. Supporting files / reports / screenshots (this repo + the generated report)
  3. How would you improve the API documentation?

Usage:
    export FORTYGUARD_API_KEY="your-key"
    python main.py              # no-credit checks: auth, validation, status, endpoint probes
    python main.py --live       # also run a full submit -> poll -> cache round trip (uses credits)
    python main.py --extended   # also run riskier validation cases (may create jobs if accepted)
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import requests

BASE_URL = os.getenv("FORTYGUARD_BASE_URL", "https://api.fortyguard.com").rstrip("/")
REQUEST_TIMEOUT = 30  # seconds per HTTP request
POLL_INTERVAL = 5  # seconds between status polls

DONE_STATUSES = {"completed", "succeeded", "success"}
FAILED_STATUSES = {"failed", "error", "cancelled", "canceled"}

# Small AOI in Lower Manhattan (~0.4 km x 0.6 km). GeoJSON order is [lon, lat].
DEFAULT_RING = [
    [-74.0170, 40.7050],
    [-74.0170, 40.7100],
    [-74.0110, 40.7100],
    [-74.0110, 40.7050],
    [-74.0170, 40.7050],
]

_DEFAULT_KEY = object()  # sentinel: "use the configured API key"


# --------------------------------------------------------------------------- #
# Data containers
# --------------------------------------------------------------------------- #
@dataclass
class Check:
    name: str
    group: str
    expectation: str
    outcome: str = "SKIP"  # PASS | FAIL | INFO | ERROR | SKIP
    status_code: Optional[int] = None
    elapsed_s: Optional[float] = None
    detail: str = ""
    note: str = ""  # candidate finding for the review form
    topic: str = ""  # "design" (Q1) or "docs" (Q3)


@dataclass
class Reply:
    response: Optional[requests.Response]
    elapsed: float
    error: str = ""

    @property
    def status(self) -> Optional[int]:
        return self.response.status_code if self.response is not None else None

    @property
    def body(self) -> Any:
        if self.response is None:
            return None
        try:
            return self.response.json()
        except ValueError:
            return None

    @property
    def headers(self) -> Any:
        return self.response.headers if self.response is not None else {}

    @property
    def snippet(self) -> str:
        if self.response is None:
            return self.error
        return self.response.text[:300].replace("\n", " ")


# --------------------------------------------------------------------------- #
# HTTP client
# --------------------------------------------------------------------------- #
class Client:
    def __init__(self, api_key: str, base_url: str = BASE_URL):
        self.api_key = api_key
        self.base_url = base_url
        self.session = requests.Session()

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        raw_body: Optional[str] = None,
        api_key: Any = _DEFAULT_KEY,
    ) -> Reply:
        headers = {"Content-Type": "application/json"}
        key = self.api_key if api_key is _DEFAULT_KEY else api_key
        if key is not None:
            headers["api-key"] = key
        start = time.perf_counter()
        try:
            resp = self.session.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                json=body if raw_body is None else None,
                data=raw_body,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            return Reply(None, time.perf_counter() - start, error=f"{type(exc).__name__}: {exc}")
        return Reply(resp, time.perf_counter() - start)


# --------------------------------------------------------------------------- #
# Payload helpers
# --------------------------------------------------------------------------- #
def with_top(p: dict, **changes: Any) -> dict:
    q = copy.deepcopy(p)
    q.update(changes)
    return q


def with_dt(p: dict, **changes: Any) -> dict:
    q = copy.deepcopy(p)
    q["date_time"].update(changes)
    return q


def without(p: dict, key: str) -> dict:
    q = copy.deepcopy(p)
    q.pop(key, None)
    return q


def with_ring(p: dict, ring: list) -> dict:
    q = copy.deepcopy(p)
    q["polygon_aoi"]["features"][0]["geometry"]["coordinates"] = [ring]
    return q


def extract_activity_id(body: Any) -> Optional[str]:
    if not isinstance(body, dict):
        return None
    if body.get("activity_id"):
        return str(body["activity_id"])
    data = body.get("data")
    if isinstance(data, dict) and data.get("activity_id"):
        return str(data["activity_id"])
    return None


def summarise_result(result: Any) -> str:
    if isinstance(result, dict):
        parts = [f"keys={sorted(result.keys())[:8]}"]
        feats = result.get("features")
        if isinstance(feats, list):
            parts.append(f"features={len(feats)}")
        return ", ".join(parts)
    if isinstance(result, list):
        return f"list of {len(result)} items"
    return f"{type(result).__name__}"


# --------------------------------------------------------------------------- #
# Test runner
# --------------------------------------------------------------------------- #
class Runner:
    def __init__(self, client: Client, args: argparse.Namespace):
        self.client = client
        self.args = args
        self.checks: list[Check] = []
        self.error_shapes: dict[tuple, list[str]] = {}
        self.headers_seen: dict[str, str] = {}
        self.out_dir = Path(args.out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)

    # ----- bookkeeping ------------------------------------------------------ #
    def payload(self) -> dict:
        p: dict[str, Any] = {
            "polygon_aoi": {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {},
                        "geometry": {"type": "Polygon", "coordinates": [DEFAULT_RING]},
                    }
                ],
            },
            # date_time must be an OBJECT (a plain ISO string returns HTTP 422).
            "date_time": {"filter_type": 3, "start_date": self.args.date},
            "granularity": self.args.granularity,
        }
        if self.args.analytic_type:
            p["analytic_type"] = self.args.analytic_type
        return p

    def record_reply(self, name: str, reply: Reply) -> None:
        if reply.status is not None and 400 <= reply.status < 500:
            body = reply.body
            shape = tuple(sorted(body)) if isinstance(body, dict) else ("<non-json>",)
            self.error_shapes.setdefault(shape, []).append(name)
        for key, value in dict(reply.headers).items():
            low = key.lower()
            if any(t in low for t in ("ratelimit", "rate-limit", "retry-after", "request-id", "credit", "deprecat", "version")):
                self.headers_seen[low] = str(value)

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        code = check.status_code if check.status_code is not None else "-"
        print(f"[{check.outcome:<5}] {check.group:<10} {check.name:<34} HTTP {code}  {check.detail[:110]}")
        return check

    @staticmethod
    def error_detail(reply: Reply) -> str:
        if reply.response is None:
            return reply.error
        body = reply.body
        if isinstance(body, dict):
            msg = str(body.get("message", ""))[:100]
            return f"error keys={sorted(body)}; message={msg!r}"
        return f"non-JSON body: {reply.snippet[:100]!r}"

    # ----- checks ----------------------------------------------------------- #
    def check_auth(self) -> None:
        cases = [
            ("auth_missing_key", None, "Request without api-key header is rejected (401/403)"),
            ("auth_invalid_key", "invalid-key-for-testing", "Request with a bad api-key is rejected (401/403)"),
        ]
        for name, key, expectation in cases:
            reply = self.client.request("POST", "/v1/heatmap", body=self.payload(), api_key=key)
            self.record_reply(name, reply)
            c = Check(name, "auth", expectation, status_code=reply.status, elapsed_s=reply.elapsed)
            c.detail = self.error_detail(reply)
            if reply.status is None:
                c.outcome = "ERROR"
            elif reply.status in (401, 403):
                c.outcome = "PASS"
            else:
                c.outcome = "FAIL"
                c.topic = "design"
                c.note = f"{name}: expected 401/403 but got HTTP {reply.status}."
            self.add(c)

    def check_validation(self) -> None:
        d = date.fromisoformat(self.args.date)
        ring_unclosed = DEFAULT_RING[:-1]
        cases: list[tuple[str, str, Callable[[dict], dict], bool]] = [
            ("empty_body", "Empty JSON object", lambda p: {}, False),
            ("missing_polygon_aoi", "polygon_aoi omitted", lambda p: without(p, "polygon_aoi"), False),
            ("missing_date_time", "date_time omitted", lambda p: without(p, "date_time"), False),
            ("date_time_is_string", "date_time sent as ISO string (original mistake)", lambda p: with_top(p, date_time="2026-11-07T12:00:00Z"), False),
            ("polygon_not_geojson", "polygon_aoi sent as WKT string", lambda p: with_top(p, polygon_aoi="POLYGON((0 0,0 1,1 1,0 0))"), False),
            ("unclosed_ring", "Polygon ring not closed", lambda p: with_ring(p, ring_unclosed), False),
            ("invalid_filter_type", "filter_type=99", lambda p: with_dt(p, filter_type=99), False),
            ("filter_type_1_no_start_time", "filter_type=1 without start_time", lambda p: with_dt(p, filter_type=1), False),
            ("filter_type_4_no_end_date", "filter_type=4 without end_date", lambda p: with_dt(p, filter_type=4), False),
            ("end_before_start", "end_date earlier than start_date", lambda p: with_dt(p, filter_type=4, end_date=(d - timedelta(days=2)).isoformat()), False),
            ("malformed_date", "start_date=14/11/2026", lambda p: with_dt(p, start_date="14/11/2026"), False),
            ("invalid_time", "start_time=25:99", lambda p: with_dt(p, filter_type=1, start_time="25:99"), False),
            ("invalid_granularity", "granularity=7 (docs list 60/80/100)", lambda p: with_top(p, granularity=7), False),
            ("invalid_analytic_type", "analytic_type='banana'", lambda p: with_top(p, analytic_type="banana"), False),
            (
                "oversized_polygon",
                "~5 x 5 degree polygon (limits vary by plan)",
                lambda p: with_ring(p, [[-80, 35], [-80, 40], [-75, 40], [-75, 35], [-80, 35]]),
                True,
            ),
            (
                "outside_us_coverage",
                "Valid polygon outside documented US coverage (Alexandria, EG)",
                lambda p: with_ring(p, [[29.90, 31.20], [29.90, 31.21], [29.91, 31.21], [29.91, 31.20], [29.90, 31.20]]),
                True,
            ),
        ]
        for name, expectation, mutate, extended in cases:
            if extended and not self.args.extended:
                self.add(Check(name, "validation", expectation, outcome="SKIP", detail="use --extended to run"))
                continue
            reply = self.client.request("POST", "/v1/heatmap", body=mutate(self.payload()))
            self.record_reply(name, reply)
            c = Check(name, "validation", f"Rejected with 400/422: {expectation}", status_code=reply.status, elapsed_s=reply.elapsed)
            c.detail = self.error_detail(reply)
            s = reply.status
            if s is None:
                c.outcome = "ERROR"
            elif s in (400, 422):
                c.outcome = "PASS"
            elif 400 <= s < 500:
                c.outcome = "INFO"
                c.detail = f"unusual rejection status {s}; " + c.detail
            elif 200 <= s < 300:
                c.outcome = "INFO" if extended else "FAIL"
                c.detail = f"ACCEPTED (HTTP {s}); response: {reply.snippet[:90]}"
                c.topic = "design"
                c.note = f"{name}: input ({expectation}) was accepted with HTTP {s} instead of being rejected; a job may have been created."
            else:
                c.outcome = "FAIL"
                c.topic = "design"
                c.note = f"{name}: server error HTTP {s} on bad input (should be a 4xx)."
            self.add(c)

        # Non-JSON payload
        reply = self.client.request("POST", "/v1/heatmap", raw_body="this is not json")
        self.record_reply("non_json_body", reply)
        c = Check("non_json_body", "validation", "Non-JSON body rejected with 400/422", status_code=reply.status, elapsed_s=reply.elapsed)
        c.detail = self.error_detail(reply)
        c.outcome = "ERROR" if reply.status is None else ("PASS" if reply.status in (400, 422) else "FAIL")
        self.add(c)

    def check_status_endpoint(self) -> None:
        for name, path_id, expectation in [
            ("status_unknown_id", str(uuid.uuid4()), "Unknown activity_id returns 404"),
            ("status_malformed_id", "not-a-valid-id", "Malformed activity_id returns 4xx"),
        ]:
            reply = self.client.request("GET", f"/v1/status/{path_id}")
            self.record_reply(name, reply)
            c = Check(name, "status", expectation, status_code=reply.status, elapsed_s=reply.elapsed)
            c.detail = self.error_detail(reply)
            if reply.status is None:
                c.outcome = "ERROR"
            elif 400 <= reply.status < 500:
                c.outcome = "PASS"
                if name == "status_unknown_id" and reply.status != 404:
                    c.outcome = "INFO"
                    c.detail = f"expected 404, got {reply.status}; " + c.detail
            else:
                c.outcome = "FAIL"
                c.topic = "design"
                c.note = f"{name}: expected a 4xx but got HTTP {reply.status}."
            self.add(c)

    def check_methods_and_probes(self) -> None:
        reply = self.client.request("GET", "/v1/heatmap")
        self.record_reply("wrong_method_get_heatmap", reply)
        self.add(
            Check(
                "wrong_method_get_heatmap",
                "methods",
                "GET on a POST-only endpoint returns 405",
                outcome="ERROR" if reply.status is None else ("PASS" if reply.status == 405 else "INFO"),
                status_code=reply.status,
                elapsed_s=reply.elapsed,
                detail=self.error_detail(reply),
            )
        )

        fake = str(uuid.uuid4())
        probes = [
            ("GET", "/v1/status", "list jobs"),
            ("GET", "/v1/activities", "list jobs (alt)"),
            ("DELETE", f"/v1/status/{fake}", "cancel job"),
            ("GET", "/v1/credits", "credits/balance"),
            ("GET", "/v1/usage", "usage"),
            ("GET", "/v1/account", "account/plan"),
            ("GET", "/v1/health", "health check"),
            ("GET", "/openapi.json", "OpenAPI spec"),
            ("GET", "/v1/openapi.json", "OpenAPI spec (versioned)"),
        ]
        for method, path, purpose in probes:
            reply = self.client.request(method, path)
            self.record_reply(f"probe {method} {path}", reply)
            c = Check(
                f"probe {method} {path}",
                "discovery",
                f"Informational: is there a '{purpose}' endpoint?",
                outcome="ERROR" if reply.status is None else "INFO",
                status_code=reply.status,
                elapsed_s=reply.elapsed,
                detail=f"{purpose}: " + (reply.snippet[:80] if reply.status is not None else reply.error),
            )
            if reply.status in (404, 405):
                c.topic = "design"
                c.note = f"No '{purpose}' endpoint found at {method} {path} (HTTP {reply.status}). Verify against the docs before reporting it as missing."
            self.add(c)

    def live_round_trip(self) -> None:
        payload = self.payload()
        reply = self.client.request("POST", "/v1/heatmap", body=payload)
        self.record_reply("live_submit", reply)
        body = reply.body
        activity_id = extract_activity_id(body)
        c = Check("live_submit", "live", "Valid request accepted (200/201/202) and returns activity_id", status_code=reply.status, elapsed_s=reply.elapsed)
        if reply.status is None:
            c.outcome, c.detail = "ERROR", reply.error
            self.add(c)
            return
        if reply.status not in (200, 201, 202):
            c.outcome, c.detail = "FAIL", self.error_detail(reply)
            self.add(c)
            return
        if activity_id:
            c.outcome, c.detail = "PASS", f"activity_id={activity_id}; response keys={sorted(body) if isinstance(body, dict) else type(body).__name__}"
            if reply.status == 200:
                c.topic = "docs"
                c.note = "Submit returned 200 rather than 202 for an asynchronous job; document the exact status codes."
        else:
            c.outcome = "INFO"
            c.detail = f"no activity_id; response looks like an inline result ({summarise_result(body)})"
        self.add(c)
        if not activity_id:
            return

        final, data, seen, elapsed = self.poll(activity_id)
        pc = Check("live_poll", "live", "Job reaches a completed status", elapsed_s=elapsed)
        pc.detail = f"final={final}; statuses seen={seen}; total {elapsed:.0f}s"
        if final == "completed":
            pc.outcome = "PASS"
            result = data.get("result", data) if isinstance(data, dict) else data
            out = self.out_dir / f"result_{activity_id}.json"
            out.write_text(json.dumps(result, indent=2), encoding="utf-8")
            pc.detail += f"; result: {summarise_result(result)}; saved to {out}"
            if any(s != s.lower() for s in seen):
                pc.topic = "docs"
                pc.note = f"Status values are mixed-case ({seen}); document the exact enum so clients can compare safely."
        elif final == "failed":
            pc.outcome = "FAIL"
            pc.detail += f"; server said: {json.dumps(data)[:200]}"
        else:
            pc.outcome = "FAIL"
            pc.topic = "design"
            pc.note = f"Job did not finish within {self.args.timeout}s; no documented ETA/timeout guidance."
        self.add(pc)

        # Identical resubmission: caching / idempotency behaviour
        again = self.client.request("POST", "/v1/heatmap", body=payload)
        self.record_reply("live_resubmit", again)
        body2 = again.body
        archived = isinstance(body2, dict) and bool(body2.get("from_archive"))
        rc = Check(
            "live_resubmit_identical",
            "live",
            "Informational: identical request is served from cache or creates a new job",
            outcome="INFO",
            status_code=again.status,
            elapsed_s=again.elapsed,
            detail=f"from_archive={archived}; new activity_id={extract_activity_id(body2)}; {again.elapsed:.1f}s",
        )
        rc.topic = "docs"
        rc.note = "Behaviour for repeated identical requests (cache hit, from_archive flag, billing) should be stated in the docs."
        self.add(rc)

    def poll(self, activity_id: str) -> tuple[str, Any, list[str], float]:
        start = time.monotonic()
        deadline = start + self.args.timeout
        seen: list[str] = []
        while time.monotonic() < deadline:
            reply = self.client.request("GET", f"/v1/status/{activity_id}")
            self.record_reply("live_poll", reply)
            if reply.status != 200:
                label = f"HTTP {reply.status}"
                if not seen or seen[-1] != label:
                    seen.append(label)
                time.sleep(POLL_INTERVAL)
                continue
            body = reply.body if isinstance(reply.body, dict) else {}
            data = body.get("data") if isinstance(body.get("data"), dict) else body
            status = str(data.get("status", "")).strip()
            if status and (not seen or seen[-1] != status):
                seen.append(status)
            low = status.lower()
            if low in DONE_STATUSES:
                return "completed", data, seen, time.monotonic() - start
            if low in FAILED_STATUSES:
                return "failed", data, seen, time.monotonic() - start
            time.sleep(POLL_INTERVAL)
        return "timeout", None, seen, time.monotonic() - start

    # ----- run + report ----------------------------------------------------- #
    def run(self) -> None:
        print(f"Target: {self.client.base_url}   AOI date: {self.args.date}\n")
        self.check_auth()
        self.check_validation()
        self.check_status_endpoint()
        self.check_methods_and_probes()
        if self.args.live:
            self.live_round_trip()
        else:
            self.add(Check("live_round_trip", "live", "Submit, poll and cache check", outcome="SKIP", detail="use --live (consumes credits)"))
        self.finalise_observations()

    def finalise_observations(self) -> None:
        if len(self.error_shapes) > 1:
            lines = "; ".join(f"{list(shape)} <- {len(names)} responses" for shape, names in self.error_shapes.items())
            self.add(
                Check(
                    "error_shape_consistency",
                    "errors",
                    "All 4xx responses share one error schema",
                    outcome="INFO",
                    detail=lines[:140],
                    topic="design",
                    note=f"4xx responses use more than one body shape: {lines}",
                )
            )
        if not any("ratelimit" in h or "rate-limit" in h or "retry-after" in h for h in self.headers_seen):
            self.add(
                Check(
                    "rate_limit_headers",
                    "headers",
                    "Rate-limit headers (X-RateLimit-*, Retry-After) are exposed",
                    outcome="INFO",
                    detail="none observed in this run",
                    topic="design",
                    note="No rate-limit headers were observed in any response of this run; clients cannot self-throttle.",
                )
            )

    def write_reports(self) -> tuple[Path, Path]:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        json_path = self.out_dir / f"report_{stamp}.json"
        md_path = self.out_dir / f"report_{stamp}.md"
        json_path.write_text(json.dumps([asdict(c) for c in self.checks], indent=2), encoding="utf-8")

        counts: dict[str, int] = {}
        for c in self.checks:
            counts[c.outcome] = counts.get(c.outcome, 0) + 1

        def esc(text: str) -> str:
            return text.replace("|", "\\|").replace("\n", " ")

        lines = [
            "# FortyGuard API test report",
            "",
            f"- Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
            f"- Base URL: `{self.client.base_url}`",
            f"- Live round trip: {'yes' if self.args.live else 'no'}; extended cases: {'yes' if self.args.extended else 'no'}",
            f"- Summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())),
            "",
            "## Results",
            "",
            "| Group | Check | Outcome | HTTP | Time (s) | Detail |",
            "|---|---|---|---|---|---|",
        ]
        for c in self.checks:
            t = f"{c.elapsed_s:.2f}" if c.elapsed_s is not None else "-"
            lines.append(f"| {c.group} | {c.name} | {c.outcome} | {c.status_code or '-'} | {t} | {esc(c.detail)[:160]} |")

        def notes(topic: str) -> list[str]:
            return [f"- {c.note}" for c in self.checks if c.topic == topic and c.note] or ["- (nothing flagged automatically)"]

        lines += [
            "",
            "## Candidate findings for the review form",
            "",
            "These are automatic hints, not conclusions. Verify each one against the docs before submitting.",
            "",
            "### Q1: API design issues or missing endpoints",
            *notes("design"),
            "",
            "### Q3: Documentation improvements",
            *notes("docs"),
            "",
            "## Response headers of interest",
            *([f"- `{k}`: {v}" for k, v in sorted(self.headers_seen.items())] or ["- none observed"]),
            "",
        ]
        md_path.write_text("\n".join(lines), encoding="utf-8")
        return md_path, json_path


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    default_date = (datetime.now(timezone.utc).date() - timedelta(days=7)).isoformat()
    p = argparse.ArgumentParser(description="Test the FortyGuard Temperature API and generate a report.")
    p.add_argument("--live", action="store_true", help="run a full submit/poll/resubmit round trip (consumes credits)")
    p.add_argument("--extended", action="store_true", help="run riskier validation cases (may create jobs if accepted)")
    p.add_argument("--date", default=default_date, help="YYYY-MM-DD for the query (default: 7 days ago; very recent data may lag)")
    p.add_argument("--granularity", type=int, default=100, choices=[60, 80, 100], help="tile edge in metres")
    p.add_argument("--analytic-type", default=None, help="optional: tcm, time_of_measure, exceedance, persistence")
    p.add_argument("--timeout", type=int, default=300, help="max seconds to wait for a job (default 300)")
    p.add_argument("--out-dir", default="reports", help="where reports and results are written")
    args = p.parse_args(argv)
    try:
        date.fromisoformat(args.date)
    except ValueError:
        p.error("--date must be YYYY-MM-DD")
    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    api_key = os.getenv("FORTYGUARD_API_KEY")
    if not api_key:
        print("Set the FORTYGUARD_API_KEY environment variable first.", file=sys.stderr)
        return 2

    runner = Runner(Client(api_key), args)
    try:
        runner.run()
    except KeyboardInterrupt:
        print("\nInterrupted; writing partial report.")
    md_path, json_path = runner.write_reports()

    failures = sum(1 for c in runner.checks if c.outcome in ("FAIL", "ERROR"))
    print(f"\nReport: {md_path}\nRaw data: {json_path}\nFailures/errors: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
