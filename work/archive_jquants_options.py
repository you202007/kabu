"""Phase 0: archive raw J-Quants Nikkei 225 options data before contract cancellation.

Context: J-Quants contract ends 2026-09-19. /derivatives/bars/daily/options/225
is the one endpoint the SQ dashboard still depends on that has no free
replacement lined up yet. This script pulls everything the current plan can
still reach and saves it locally, unmodified, before that access disappears.

CRITICAL — data placement:
This script NEVER writes under this repository. J-Quants terms of service
prohibit distributing/sharing the raw data in any third-party-viewable form,
and this repo is public. --archive-dir defaults to a folder under the user's
home directory, entirely outside any git working tree. Do not change that
default to a path inside this repo, and do not git add/commit anything it
writes.

What "raw" means here: the JSON records returned by the API's data[] array,
saved as-is (field names, values, ordering untouched) — not the aggregated
sq_scores.csv columns the rest of this pipeline computes. If the aggregation
logic changes later, this archive can still be re-processed from scratch.

Resumability: every date's fetch result (ok / no_data / error) is recorded in
manifest.csv keyed by date. Rerunning the script only fetches dates that are
missing or still marked "error" — dates already "ok" or "no_data" are never
re-requested, so raw/*.json.gz is append-only in practice, and progress
survives an interrupted run.

Rate limiting: J-Quants Standard allows 120 req/min. This script paces
requests at --requests-per-minute (default 90, leaving headroom for retries)
rather than relying solely on the client library's built-in retry, and never
uses the library's ThreadPoolExecutor-based *_range() helpers (those fire one
concurrent request per calendar day in the range and are what caused the
429 failure in the main dashboard pipeline for even a 90-day window).
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import random
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from jquantsapi.client_v2 import ClientV2

ENDPOINT = "/derivatives/bars/daily/options/225"
DEFAULT_ARCHIVE_DIR = Path.home() / "Documents" / "jquants_options_archive"
MANIFEST_COLUMNS = ["date", "status", "record_count", "fetched_at", "error"]


def load_dotenv_key(repo_root: Path) -> str | None:
    env_path = repo_root / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "JQUANTS_API_KEY":
            return value.strip() or None
    return None


def resolve_api_key(cli_value: str | None, repo_root: Path) -> str:
    key = cli_value or os.environ.get("JQUANTS_API_KEY") or load_dotenv_key(repo_root)
    if not key:
        raise RuntimeError(
            "JQUANTS_API_KEY not found. Set it in the environment, pass --api-key, "
            "or put JQUANTS_API_KEY=... in a local .env file (git-ignored) at the repo root."
        )
    return key


def load_manifest(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    df = pd.read_csv(path, dtype=str)
    return {row["date"]: row.to_dict() for _, row in df.iterrows()}


def save_manifest(path: Path, rows: dict[str, dict]) -> None:
    if not rows:
        pd.DataFrame(columns=MANIFEST_COLUMNS).to_csv(path, index=False, encoding="utf-8")
        return
    df = pd.DataFrame(list(rows.values()))
    for col in MANIFEST_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df = df[MANIFEST_COLUMNS].sort_values("date")
    df.to_csv(path, index=False, encoding="utf-8")


def fetch_one(
    client: ClientV2,
    d: date,
    raw_dir: Path,
    max_retries: int,
) -> dict:
    key = d.isoformat()
    yyyymmdd = d.strftime("%Y%m%d")
    attempt = 0
    while True:
        attempt += 1
        try:
            records = client._get_paginated(ENDPOINT, params={"date": yyyymmdd})  # noqa: SLF001
            out_path = raw_dir / f"{key}.json.gz"
            payload = {
                "date": key,
                "endpoint": ENDPOINT,
                "fetched_at": pd.Timestamp.now("UTC").isoformat(),
                "record_count": len(records),
                "records": records,
            }
            with gzip.open(out_path, "wt", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            return {
                "date": key,
                "status": "ok" if records else "no_data",
                "record_count": len(records),
                "fetched_at": payload["fetched_at"],
                "error": "",
            }
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "Your subscription covers" in msg:
                # Permanent plan-boundary rejection, not a transient failure — retrying
                # won't help. Record it distinctly so a rerun doesn't loop on it forever
                # and so it isn't confused with a genuine fetch error worth investigating.
                return {
                    "date": key,
                    "status": "out_of_plan_range",
                    "record_count": 0,
                    "fetched_at": pd.Timestamp.now("UTC").isoformat(),
                    "error": msg[:300],
                }
            is_429 = "429" in msg
            if attempt >= max_retries:
                return {
                    "date": key,
                    "status": "error",
                    "record_count": 0,
                    "fetched_at": pd.Timestamp.now("UTC").isoformat(),
                    "error": msg[:300],
                }
            backoff = (35 if is_429 else 6) * attempt + random.uniform(0, 3)
            print(f"  {key}: {'429 rate-limited' if is_429 else 'error'}, retry {attempt}/{max_retries} in {backoff:.0f}s ({msg[:120]})")
            time.sleep(backoff)


def daterange_weekdays(start: date, end: date) -> list[date]:
    out = []
    d = start
    while d <= end:
        if d.weekday() < 5:  # skip Sat/Sun without spending an API call
            out.append(d)
        d += timedelta(days=1)
    return out


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Archive raw J-Quants Nikkei 225 options data locally.")
    parser.add_argument("--start", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(), default=None)
    parser.add_argument("--end", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(), default=None)
    parser.add_argument("--years-back", type=int, default=10, help="Used to derive --start when not given.")
    parser.add_argument("--archive-dir", type=Path, default=DEFAULT_ARCHIVE_DIR)
    parser.add_argument("--api-key")
    parser.add_argument("--requests-per-minute", type=float, default=90.0)
    parser.add_argument("--max-retries", type=int, default=5)
    return parser.parse_args()


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows console defaults to cp932
    except AttributeError:
        pass
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]

    if str(args.archive_dir.resolve()).startswith(str(repo_root.resolve())):
        raise RuntimeError(
            f"Refusing to write archive under the repository ({repo_root}). "
            "J-Quants ToS forbids storing this data anywhere third parties can view it, "
            "and this repo is public. Choose --archive-dir outside the repo."
        )

    end = args.end or (date.today() - timedelta(days=1))
    start = args.start or (pd.Timestamp(end) - pd.DateOffset(years=args.years_back)).date()

    raw_dir = args.archive_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.archive_dir / "manifest.csv"
    manifest = load_manifest(manifest_path)

    api_key = resolve_api_key(args.api_key, repo_root)
    client = ClientV2(api_key=api_key)

    all_dates = daterange_weekdays(start, end)
    todo = [d for d in all_dates if manifest.get(d.isoformat(), {}).get("status") not in ("ok", "no_data", "out_of_plan_range")]

    print(f"range: {start} .. {end} ({len(all_dates)} weekdays)")
    print(f"already done: {len(all_dates) - len(todo)} / remaining: {len(todo)}")
    print(f"archive dir: {args.archive_dir}")

    min_interval = 60.0 / args.requests_per_minute
    for i, d in enumerate(todo):
        t0 = time.monotonic()
        result = fetch_one(client, d, raw_dir, args.max_retries)
        manifest[result["date"]] = result
        if (i + 1) % 25 == 0 or (i + 1) == len(todo):
            save_manifest(manifest_path, manifest)
            done = sum(1 for r in manifest.values() if r["status"] in ("ok", "no_data", "out_of_plan_range"))
            errors = sum(1 for r in manifest.values() if r["status"] == "error")
            print(f"progress: {i + 1}/{len(todo)} this run | manifest total ok/no_data={done} error={errors}")
        elapsed = time.monotonic() - t0
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)

    save_manifest(manifest_path, manifest)

    rows = [manifest[d.isoformat()] for d in all_dates if d.isoformat() in manifest]
    ok = [r for r in rows if r["status"] == "ok"]
    no_data = [r for r in rows if r["status"] == "no_data"]
    out_of_plan = [r for r in rows if r["status"] == "out_of_plan_range"]
    errors = [r for r in rows if r["status"] == "error"]
    total_records = sum(int(r["record_count"]) for r in ok)

    print("\n=== summary ===")
    print(f"requested weekday range: {start} .. {end} ({len(all_dates)} days)")
    print(f"ok (has data): {len(ok)}")
    print(f"no_data (queried, empty - likely holiday): {len(no_data)}")
    print(f"out_of_plan_range (before what Standard can reach - not a gap): {len(out_of_plan)}")
    print(f"error (needs retry - rerun this script to resume): {len(errors)}")
    print(f"total raw records archived: {total_records}")
    if errors:
        print("error dates:", ", ".join(r["date"] for r in errors[:30]), "..." if len(errors) > 30 else "")


if __name__ == "__main__":
    main()
