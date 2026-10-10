from __future__ import annotations

import time

import httpx


def wait_until_up(*urls: str, timeout: float = 180) -> None:
    """Wait for every URL to answer 200 (containers can be 'started' before they serve)."""
    deadline = time.monotonic() + timeout
    for url in urls:
        while True:
            try:
                if httpx.get(url, timeout=3).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise TimeoutError(f"{url} did not come up")
            time.sleep(2)
