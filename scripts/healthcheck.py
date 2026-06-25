"""Lightweight HTTP healthcheck used by both the CI smoke job and as a
fallback Docker HEALTHCHECK (the image's default `curl` invocation in the
Dockerfile is faster, but on hosts without curl this script does the same
work with just the Python stdlib).

Exit codes:
  0 — endpoint returned 2xx within `timeout`
  1 — endpoint unreachable, timed out, or returned non-2xx

Usage:
  python scripts/healthcheck.py                # default: http://localhost:8050/_dash-layout
  python scripts/healthcheck.py http://host/_dash-layout --retries 30 --interval 2
"""
from __future__ import annotations

import argparse
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

DEFAULT_URL = "http://localhost:8050/_dash-layout"


def _ping(url: str, timeout: float) -> bool:
    """Return True iff GET `url` returns a 2xx within `timeout` seconds."""
    req = Request(url, headers={"User-Agent": "hk-sentiment-healthcheck/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (URLError, TimeoutError, ConnectionError, OSError):
        return False


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("url", nargs="?", default=DEFAULT_URL,
                   help=f"endpoint to GET (default: {DEFAULT_URL})")
    p.add_argument("--retries", type=int, default=1,
                   help="max attempts before giving up (default: 1)")
    p.add_argument("--interval", type=float, default=2.0,
                   help="seconds between retries (default: 2)")
    p.add_argument("--timeout", type=float, default=10.0,
                   help="per-request timeout in seconds (default: 10)")
    args = p.parse_args()

    for attempt in range(1, args.retries + 1):
        if _ping(args.url, args.timeout):
            return 0
        if attempt < args.retries:
            time.sleep(args.interval)
    sys.stderr.write(
        f"healthcheck failed: {args.url} did not return 2xx "
        f"after {args.retries} attempt(s)\n"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
