"""Tiny stdlib-only HTTP helper (no pip install needed in CI)."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

USER_AGENT = "nba-odds/1.0 (+personal research tool)"


class FetchError(RuntimeError):
    pass


def get_json(
    url: str,
    params: dict[str, Any] | None = None,
    *,
    retries: int = 4,
    timeout: float = 20.0,
    with_headers: bool = False,
) -> Any:
    """GET a URL and parse JSON. Retries on network errors and 429/5xx."""
    if params:
        url = f"{url}?{urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})}"
    last_err: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return (body, dict(resp.headers)) if with_headers else body
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code not in (429, 500, 502, 503, 504):
                break
            if e.code == 429:  # rate limited: honour Retry-After, else back off exponentially
                try:
                    wait = float(e.headers.get("Retry-After") or 0)
                except ValueError:
                    wait = 0
                time.sleep(max(wait, 2.0 * 2 ** attempt))
                continue
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = e
        time.sleep(1.5 * (attempt + 1))
    # Never echo query strings: they may carry an API key.
    safe = url.split("?", 1)[0]
    raise FetchError(f"GET {safe} failed: {last_err}")
