"""A fake API that misbehaves in every way we know of.

Real APIs do all of this sooner or later, by accident or on purpose: they repeat the
request they were sent (key included), answer with broken or enormous JSON, redirect in
circles, claim rate limits that make no sense, or do not answer at all. Each behaviour
here is one handler for the fake HTTP transport; `tests/test_hostile_api.py` runs every
one of them through every way an agent can reach librarian.

A handler works for any request, so the same behaviour can answer a read and a write.
"""

from __future__ import annotations

import gzip
import html
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from tests.conftest import BASE

READ_KEY = "r3ad-key-0123456789"
WRITE_KEY = "wr1te-key-9876543210"
KEYS = (READ_KEY, WRITE_KEY)

Handler = Callable[[httpx.Request], httpx.Response]


def sent(request: httpx.Request) -> str:
    """The credential this request carries."""
    return request.headers.get("authorization", "").removeprefix("Bearer ")


def json_bytes(text: str, status: int = 200) -> httpx.Response:
    """An answer whose body is exactly this text, declared as JSON."""
    return httpx.Response(
        status, content=text.encode(), headers={"content-type": "application/json"}
    )


# -- it repeats what it was sent --------------------------------------------------------


def echo_json(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"headers": dict(request.headers), "url": str(request.url)})


def echo_text(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text=f"you sent Authorization: {request.headers['authorization']}")


def echo_in_keys(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={sent(request): {"seen": 1}, "by": [{sent(request): True}]})


def echo_error_page(request: httpx.Request) -> httpx.Response:
    page = (
        f"<h1>Not Found</h1><p>{html.escape(str(request.url))} with key "
        f"{html.escape(sent(request))} was not found.</p>"
    )
    return httpx.Response(404, text=page, headers={"content-type": "text/html"})


def echo_forbidden(request: httpx.Request) -> httpx.Response:
    return httpx.Response(403, json={"message": f"token {sent(request)} may not do this"})


def echo_server_error(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, json={"error": {"detail": f"crashed handling {sent(request)}"}})


def echo_in_headers(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={"ok": True},
        headers={"sunset": f"never, says {sent(request)}", "deprecation": sent(request)},
    )


def echo_cut_off(request: httpx.Request) -> httpx.Response:
    """With the size limit set to 100, the answer ends in the middle of the key."""
    lead = "x" * (100 - len("Bearer ") - 9)
    return httpx.Response(200, text=f"{lead}Bearer {sent(request)} and more")


def echo_after_redirect(request: httpx.Request) -> httpx.Response:
    if request.url.params.get("again"):
        return httpx.Response(200, json={"was": request.url.params["again"], "now": sent(request)})
    target = request.url.copy_merge_params({"again": sent(request)})
    return httpx.Response(307, headers={"location": str(target)})


# -- it answers with something that is not what it says ------------------------------


def very_deep_json(request: httpx.Request) -> httpx.Response:
    return json_bytes("[" * 100_000 + "]" * 100_000)


def deep_json(request: httpx.Request) -> httpx.Response:
    return json_bytes('{"a":' * 400 + "1" + "}" * 400)


def not_a_number(request: httpx.Request) -> httpx.Response:
    return json_bytes('{"a": NaN, "b": Infinity, "c": -Infinity, "d": 1e999, "e": [NaN]}')


def lone_surrogate(request: httpx.Request) -> httpx.Response:
    return json_bytes('{"name": "half a pair: \\ud83d", "\\udc00": 1}')


def nul_character(request: httpx.Request) -> httpx.Response:
    return json_bytes('{"name": "a\\u0000b", "k\\u0000": "v"}')


def huge_integer(request: httpx.Request) -> httpx.Response:
    return json_bytes('{"id": 123456789012345678901234567890, "small": 7}')


def endless_number(request: httpx.Request) -> httpx.Response:
    return json_bytes('{"id": ' + "9" * 6000 + "}")


def broken_json(request: httpx.Request) -> httpx.Response:
    return json_bytes('{"a": [1, 2,')


def empty_json(request: httpx.Request) -> httpx.Response:
    return json_bytes("")


def json_null(request: httpx.Request) -> httpx.Response:
    return json_bytes("null")


def json_string(request: httpx.Request) -> httpx.Response:
    return json_bytes('"just a string"')


def binary(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, content=bytes(range(256)) * 8, headers={"content-type": "application/octet-stream"}
    )


def enormous(request: httpx.Request) -> httpx.Response:
    return json_bytes('{"rows": [' + ",".join(["1234567890"] * 600_000) + "]}")


def compressed_bomb(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        content=gzip.compress(b'{"zeros": "' + b"0" * 20_000_000 + b'"}'),
        headers={"content-encoding": "gzip", "content-type": "application/json"},
    )


def terminal_escapes(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, text="boom\x1b[2J\x1b[31m\r\nINFO forged log line\x07")


# -- it plays with the protocol ---------------------------------------------------------


def redirect_forever(request: httpx.Request) -> httpx.Response:
    hop = int(request.url.params.get("hop", "0")) + 1
    return httpx.Response(302, headers={"location": f"{request.url.path}?hop={hop}"})


def redirect_elsewhere(request: httpx.Request) -> httpx.Response:
    return httpx.Response(307, headers={"location": "https://elsewhere.example.test/steal"})


def unauthorized(request: httpx.Request) -> httpx.Response:
    return httpx.Response(401, json={"message": f"bad credentials: {sent(request)}"})


def rate_limit_overflow(request: httpx.Request) -> httpx.Response:
    return httpx.Response(429, json={}, headers={"retry-after": "9" * 30})


def rate_limit_nonsense(request: httpx.Request) -> httpx.Response:
    return httpx.Response(429, text="slow down", headers={"retry-after": "-5, or whenever"})


def rate_limit_far_future(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        403,
        json={"message": "API rate limit exceeded"},
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9" * 25},
    )


def rate_limit_bad_date(request: httpx.Request) -> httpx.Response:
    return httpx.Response(429, json={}, headers={"retry-after": "Mon, 99 Foo 0000 99:99:99 GMT"})


def no_content_with_body(request: httpx.Request) -> httpx.Response:
    return httpx.Response(204, content=b"surprise", headers={"content-type": "application/json"})


def endless_pages(request: httpx.Request) -> httpx.Response:
    page = int(request.url.params.get("page", "1")) + 1
    link = f'<{BASE}{request.url.path}?page={page}>; rel="next"'
    return httpx.Response(200, json=[sent(request)], headers={"link": link})


# -- it does not answer -----------------------------------------------------------------


def unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("All connection attempts failed")


def connect_timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectTimeout("timed out")


def never_answers(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("timed out")


def hangs_up(request: httpx.Request) -> httpx.Response:
    raise httpx.RemoteProtocolError("peer closed connection without sending complete message")


def breaks_off(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadError("connection reset by peer")


def bad_compression(request: httpx.Request) -> httpx.Response:
    raise httpx.DecodingError("Error -3 while decompressing data: incorrect header check")


def no_free_connection(request: httpx.Request) -> httpx.Response:
    raise httpx.PoolTimeout("timed out waiting for a connection")


@dataclass(frozen=True)
class Behaviour:
    handler: Handler
    # Whether the API got (or may have got) the request. False: it certainly did not,
    # so a write was not made.
    reached: bool = True
    # The answer never arrives: the agent must be told so in words it can act on.
    silent: bool = False
    # The size limit to run with, when the behaviour is about being cut off.
    limit: int | None = None


BEHAVIOURS: dict[str, Behaviour] = {
    "echo_json": Behaviour(echo_json),
    "echo_text": Behaviour(echo_text),
    "echo_in_keys": Behaviour(echo_in_keys),
    "echo_error_page": Behaviour(echo_error_page),
    "echo_forbidden": Behaviour(echo_forbidden),
    "echo_server_error": Behaviour(echo_server_error),
    "echo_in_headers": Behaviour(echo_in_headers),
    "echo_cut_off": Behaviour(echo_cut_off, limit=100),
    "echo_after_redirect": Behaviour(echo_after_redirect),
    "very_deep_json": Behaviour(very_deep_json),
    "deep_json": Behaviour(deep_json),
    "not_a_number": Behaviour(not_a_number),
    "lone_surrogate": Behaviour(lone_surrogate),
    "nul_character": Behaviour(nul_character),
    "huge_integer": Behaviour(huge_integer),
    "endless_number": Behaviour(endless_number),
    "broken_json": Behaviour(broken_json),
    "empty_json": Behaviour(empty_json),
    "json_null": Behaviour(json_null),
    "json_string": Behaviour(json_string),
    "binary": Behaviour(binary),
    "enormous": Behaviour(enormous),
    "compressed_bomb": Behaviour(compressed_bomb),
    "terminal_escapes": Behaviour(terminal_escapes),
    "redirect_forever": Behaviour(redirect_forever),
    "redirect_elsewhere": Behaviour(redirect_elsewhere),
    "unauthorized": Behaviour(unauthorized),
    "rate_limit_overflow": Behaviour(rate_limit_overflow),
    "rate_limit_nonsense": Behaviour(rate_limit_nonsense),
    "rate_limit_far_future": Behaviour(rate_limit_far_future),
    "rate_limit_bad_date": Behaviour(rate_limit_bad_date),
    "no_content_with_body": Behaviour(no_content_with_body),
    "endless_pages": Behaviour(endless_pages),
    "unreachable": Behaviour(unreachable, reached=False, silent=True),
    "connect_timeout": Behaviour(connect_timeout, reached=False, silent=True),
    "no_free_connection": Behaviour(no_free_connection, reached=False, silent=True),
    "never_answers": Behaviour(never_answers, silent=True),
    "hangs_up": Behaviour(hangs_up, silent=True),
    "breaks_off": Behaviour(breaks_off, silent=True),
    "bad_compression": Behaviour(bad_compression, silent=True),
}
