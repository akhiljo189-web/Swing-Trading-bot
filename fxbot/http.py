"""Minimal JSON-over-HTTPS transport (standard library only), injectable for tests."""
import json
import urllib.error
import urllib.request
from collections.abc import Callable

# (method, url, headers, json_body_or_None, timeout_seconds) -> (status_code, parsed_json_or_{})
Transport = Callable[[str, str, dict, dict | None, float], tuple[int, dict]]


class HttpError(Exception):
    """The request did not produce an HTTP response (network failure, timeout, bad JSON)."""


def urllib_transport(method: str, url: str, headers: dict, body: dict | None,
                     timeout: float) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw, status = resp.read(), resp.status
    except urllib.error.HTTPError as exc:      # 4xx/5xx still carry a JSON body worth reading
        raw, status = exc.read(), exc.code
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HttpError(str(exc)) from exc
    try:
        return status, (json.loads(raw) if raw else {})
    except ValueError as exc:
        raise HttpError(f"invalid JSON from {url}") from exc
