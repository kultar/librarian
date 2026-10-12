"""Finding an API's documentation when nobody said where it is.

Given only the API's base URL (and maybe a name), look where documentation usually is
(`/llms.txt`, `/docs`, `docs.<domain>`, links on the homepage) and, if configured, search
the web. Every candidate page is fetched (SSRF-guarded) and scored by how much it looks
like API reference documentation; the best one is handed to the docs learner.

Search results are untrusted: anyone can publish a page called "Acme API docs". Sources
are therefore ranked by trust, and third-party sites are excluded by default:

- official:  the API's own domain (api.acme.com -> acme.com, docs.acme.com, ...)
- docs-host: a well-known documentation host (GitHub, ReadMe, GitBook, Postman, ...)
- third-party: anything else

Whatever is found, calls still only ever go to the base URL the onboarding caller gave.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, cast
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import BaseModel, Field

from librarian.docs import html_to_text
from librarian.net import PUBLIC, NetworkPolicy
from librarian.openapi import SpecError, fetch_text, parse_spec

log = logging.getLogger(__name__)

Trust = Literal["official", "docs-host", "third-party"]
FoundBy = Literal["well-known", "homepage", "search"]

DOCS_PATHS = (
    "/llms.txt",
    "/docs",
    "/docs/api",
    "/api/docs",
    "/developers",
    "/developer",
    "/reference",
    "/api-reference",
    "/api",
)
DOCS_SUBDOMAINS = ("docs", "developer", "developers", "api-docs")
DOCS_HOSTS = (
    "github.com",
    "raw.githubusercontent.com",
    "readme.io",
    "readme.com",
    "gitbook.io",
    "gitbook.com",
    "postman.com",
    "getpostman.com",
    "apiary.io",
    "stoplight.io",
    "redocly.com",
    "swaggerhub.com",
    "readthedocs.io",
)
# Second-level labels under which registrations happen one level deeper (acme.co.uk).
_SECOND_LEVEL = frozenset({"co", "com", "net", "org", "gov", "ac", "edu", "or", "ne", "go"})
_METHOD_PATH = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(https?://\S+|/[\w{}:<>\-./]+)")
_LINK_WORDS = re.compile(r"(api|developer|docs?|documentation|reference)", re.I)
_DOCS_PATH = re.compile(r"(docs?|documentation|reference)", re.I)
MIN_SCORE = 3
MAX_CANDIDATES = 16
MAX_DOC_BYTES = 3_000_000


def registrable_domain(host: str) -> str:
    """acme.com for api.acme.com; acme.co.uk for api.acme.co.uk (a heuristic, no PSL)."""
    labels = host.lower().strip(".").split(".")
    if len(labels) <= 2:
        return ".".join(labels)
    if len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def trust_of(url: str, base_url: str) -> Trust:
    host = (urlsplit(url).hostname or "").lower()
    own = registrable_domain(urlsplit(base_url).hostname or "")
    if host == own or host.endswith("." + own):
        return "official"
    if any(host == h or host.endswith("." + h) for h in DOCS_HOSTS):
        return "docs-host"
    return "third-party"


_TRUST_RANK: dict[str, int] = {"official": 2, "docs-host": 1, "third-party": 0}


@dataclass
class FoundDocs:
    url: str
    found_by: FoundBy
    trust: Trust
    score: int
    spec: dict[str, Any] | None = None  # the page was itself an OpenAPI document


@dataclass
class Candidate:
    url: str
    found_by: FoundBy


Searcher = Callable[[str], Awaitable[list[str]]]
"""Runs a web search; returns result URLs, best first."""


def docs_score(text: str, base_url: str) -> int:
    """How much a page looks like reference docs for this API (method+path lines,
    or full URLs on the API's host)."""
    base = urlsplit(base_url)
    score = len(_METHOD_PATH.findall(text))
    if base.hostname:
        score += min(text.count(f"{base.scheme}://{base.netloc}"), 20)
    return score


async def find_docs(
    base_url: str,
    client: httpx.AsyncClient,
    *,
    name: str | None = None,
    searcher: Searcher | None = None,
    net: NetworkPolicy = PUBLIC,
    allow_third_party: bool = False,
    look_nearby: bool = True,
) -> tuple[FoundDocs | None, list[str]]:
    """Find the best documentation page. Returns (found, notes on what was tried)."""
    notes: list[str] = []
    if look_nearby:
        nearby = _nearby_candidates(base_url)
        onward: list[Candidate] = []
        found = await _best(nearby, base_url, client, net, allow_third_party, len(nearby), onward)
        if found is None:
            homepage = await _homepage_links(base_url, client, net)
            found = await _best(homepage, base_url, client, net, allow_third_party, follow=onward)
        if found is None:
            # Landing pages often link to the reference one click further.
            found = await _best(onward, base_url, client, net, allow_third_party)
        if found is not None:
            return found, notes
        notes.append("no documentation in the usual places")
    if searcher is None:
        return None, notes
    query = f"{name or registrable_domain(urlsplit(base_url).hostname or '')} API documentation"
    try:
        urls = await searcher(query)
    except Exception as exc:  # a broken search must not break onboarding
        log.warning("docs search failed: %s", exc)
        notes.append(f"search failed: {exc}")
        return None, notes
    candidates = [Candidate(u, "search") for u in urls]
    skipped = (
        [c.url for c in candidates if trust_of(c.url, base_url) == "third-party"]
        if not allow_third_party
        else []
    )
    if skipped:
        notes.append(
            f"skipped {len(skipped)} third-party search result(s) "
            "(set learning.search.allow_third_party to use them)"
        )
    found = await _best(candidates, base_url, client, net, allow_third_party)
    if found is None:
        notes.append(f"search for {query!r} found no usable documentation")
    return found, notes


def _nearby_candidates(base_url: str) -> list[Candidate]:
    parts = urlsplit(base_url)
    domain = registrable_domain(parts.hostname or "")
    origins = [f"{parts.scheme}://{parts.netloc}"]
    for host in (domain, *(f"{sub}.{domain}" for sub in DOCS_SUBDOMAINS)):
        origin = f"https://{host}"
        if origin not in origins:
            origins.append(origin)
    found: list[Candidate] = []
    for origin in origins:
        is_docs_host = urlsplit(origin).hostname != parts.hostname and any(
            (urlsplit(origin).hostname or "").startswith(f"{sub}.") for sub in DOCS_SUBDOMAINS
        )
        paths = ("/", "/llms.txt") if is_docs_host else DOCS_PATHS
        found.extend(Candidate(origin + path, "well-known") for path in paths)
    return found


async def _homepage_links(
    base_url: str, client: httpx.AsyncClient, net: NetworkPolicy
) -> list[Candidate]:
    domain = registrable_domain(urlsplit(base_url).hostname or "")
    home = f"https://{domain}/"
    try:
        raw, final = await fetch_text(home, client, net=net, max_bytes=MAX_DOC_BYTES)
    except (SpecError, httpx.HTTPError):
        return []
    _, links = html_to_text(raw)
    candidates: list[Candidate] = []
    for href in links:
        url = urljoin(final, href).split("#", 1)[0]
        if (
            urlsplit(url).scheme in ("http", "https")
            and _LINK_WORDS.search(url)
            and all(c.url != url for c in candidates)
        ):
            candidates.append(Candidate(url, "homepage"))
    return candidates[:MAX_CANDIDATES]


async def _best(
    candidates: list[Candidate],
    base_url: str,
    client: httpx.AsyncClient,
    net: NetworkPolicy,
    allow_third_party: bool,
    limit: int = MAX_CANDIDATES,
    follow: list[Candidate] | None = None,
) -> FoundDocs | None:
    """The best-looking docs page among the candidates. Docs-looking links on pages that
    are not docs themselves are added to `follow` (official site only)."""
    best: FoundDocs | None = None
    seen: set[str] = set()
    for candidate in candidates[:limit]:
        if candidate.url in seen:
            continue
        seen.add(candidate.url)
        trust = trust_of(candidate.url, base_url)
        if trust == "third-party" and not allow_third_party:
            continue
        try:
            raw, final = await fetch_text(candidate.url, client, net=net, max_bytes=MAX_DOC_BYTES)
        except (SpecError, httpx.HTTPError):
            continue
        trust = trust_of(final, base_url)  # a redirect can leave the official site
        if trust == "third-party" and not allow_third_party:
            continue
        try:
            spec = parse_spec(raw)
        except SpecError:
            spec = None
        if spec is not None:
            return FoundDocs(final, candidate.found_by, trust, 1000, spec)
        text, links = html_to_text(raw)
        score = docs_score(text, base_url)
        # A docs index often shows few examples itself (they are on its sub-pages, which
        # the learner reads anyway): accept a trusted page that says it is the docs.
        looks_like_docs = trust != "third-party" and bool(_DOCS_PATH.search(urlsplit(final).path))
        if score < MIN_SCORE and not (score >= 1 and looks_like_docs):
            if follow is not None and trust == "official":
                for href in links:
                    url = urljoin(final, href).split("#", 1)[0]
                    if (
                        _LINK_WORDS.search(urlsplit(url).path)
                        and trust_of(url, base_url) == "official"
                        and url not in seen
                        and all(c.url != url for c in follow)
                        and len(follow) < MAX_CANDIDATES
                    ):
                        follow.append(Candidate(url, candidate.found_by))
            continue
        found = FoundDocs(final, candidate.found_by, trust, score)
        if best is None or (_TRUST_RANK[found.trust], found.score) > (
            _TRUST_RANK[best.trust],
            best.score,
        ):
            best = found
    return best


# -- searchers ----------------------------------------------------------------------


def searxng_searcher(url: str, client: httpx.AsyncClient, max_results: int) -> Searcher:
    async def search(query: str) -> list[str]:
        response = await client.get(
            url.rstrip("/") + "/search", params={"q": query, "format": "json"}
        )
        response.raise_for_status()
        results = cast(list[dict[str, Any]], response.json().get("results") or [])
        return [str(r["url"]) for r in results if r.get("url")][:max_results]

    return search


def brave_searcher(api_key: str, client: httpx.AsyncClient, max_results: int) -> Searcher:
    async def search(query: str) -> list[str]:
        response = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": max_results},
            headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
        )
        response.raise_for_status()
        web = cast(dict[str, Any], response.json().get("web") or {})
        results = cast(list[dict[str, Any]], web.get("results") or [])
        return [str(r["url"]) for r in results if r.get("url")][:max_results]

    return search


class SearchResults(BaseModel):
    urls: list[str] = Field(
        default_factory=list[str],
        description="URLs of the API's official documentation pages, best first",
    )


SEARCH_INSTRUCTIONS = """\
Use web search to find the official documentation for the API named in the request:
reference pages that list its HTTP endpoints, or its OpenAPI/Swagger file. Prefer pages on
the API's own domain. Return the URLs only; do not describe the API.
"""


_NO_SEARCH = (
    "Web search needs an Anthropic model: through a gateway such as LiteLLM, set "
    "model_api: anthropic. Or use learning.search.provider: searxng or brave."
)


class SearchDidNotRun(Exception):
    """The model answered without searching (e.g. a gateway dropped the search tool)."""


def model_searcher(model: Any, max_results: int) -> Searcher:
    """Search with the model's own web search tool.

    Some gateways accept the request but silently drop the search tool, and the model then
    answers from memory. That is detected (no search call in the reply) and refused.
    """
    from pydantic_ai import Agent
    from pydantic_ai.capabilities import WebSearch
    from pydantic_ai.exceptions import UserError
    from pydantic_ai.messages import ModelResponse, NativeToolCallPart

    agent: Agent[None, SearchResults] = Agent(
        model,
        output_type=SearchResults,
        instructions=SEARCH_INSTRUCTIONS,
        capabilities=[WebSearch(max_uses=3)],
        name="librarian-docs-search",
    )

    async def search(query: str) -> list[str]:
        try:
            result = await agent.run(query)
        except UserError as exc:  # this model/API format has no web search
            raise SearchDidNotRun(f"{_NO_SEARCH} ({exc})") from exc
        searched = any(
            isinstance(part, NativeToolCallPart)
            for message in result.all_messages()
            if isinstance(message, ModelResponse)
            for part in message.parts
        )
        if not searched:
            raise SearchDidNotRun(f"the model answered without searching. {_NO_SEARCH}")
        return [u for u in result.output.urls if u.startswith(("http://", "https://"))][
            :max_results
        ]

    return search
