"""Finding documentation when nobody says where it is: usual places, homepage, search."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from librarian.calls import Caller
from librarian.config import SearchSettings
from librarian.docs import build_extractor
from librarian.finddocs import find_docs, registrable_domain, trust_of
from librarian.net import NetworkPolicy
from librarian.service import Librarian, LibrarianError
from tests.conftest import Router
from tests.test_learning import extractor_model
from tests.test_openapi import SPEC

BASE = "https://api.gizmo.test/v1"
AGENT = Caller(agent="agent-a")
HTML = {"content-type": "text/html"}

DOCS_PAGE = """<html><body><h1>Gizmo API reference</h1>
<pre>GET /v1/gizmos</pre><p>List gizmos.</p>
<pre>GET /v1/gizmos/{id}</pre><p>One gizmo.</p>
<pre>DELETE /v1/gizmos/{id}</pre><p>Delete one.</p>
</body></html>"""
MARKETING = "<html><body><h1>Gizmo: the best gizmos</h1></body></html>"


PRIVATE = NetworkPolicy(allow_private=True)


def client(router: Router) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(router))


def page(text: str) -> Callable[[httpx.Request], httpx.Response]:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=text, headers=HTML)

    return respond


def test_registrable_domain_and_trust() -> None:
    assert registrable_domain("api.gizmo.test") == "gizmo.test"
    assert registrable_domain("api.gizmo.co.uk") == "gizmo.co.uk"
    assert registrable_domain("dog.ceo") == "dog.ceo"
    assert trust_of("https://docs.gizmo.test/x", BASE) == "official"
    assert trust_of("https://gizmo.readme.io/x", BASE) == "docs-host"
    assert trust_of("https://github.com/gizmo/api", BASE) == "docs-host"
    assert trust_of("https://gizmo-api-docs.example/x", BASE) == "third-party"
    assert trust_of("https://evilgizmo.test/x", BASE) == "third-party"  # not a subdomain


async def test_finds_docs_on_a_docs_subdomain(router: Router) -> None:
    router.add("GET", "https://gizmo.test/docs", page(MARKETING))  # not docs: no endpoints
    router.add("GET", "https://docs.gizmo.test/", page(DOCS_PAGE))
    found, _ = await find_docs(BASE, client(router), net=PRIVATE)
    assert found is not None
    assert (found.url, found.found_by, found.trust) == (
        "https://docs.gizmo.test/",
        "well-known",
        "official",
    )


async def test_finds_docs_linked_from_the_homepage(router: Router) -> None:
    home = "<html><body><a href='/build/api-reference'>Developers</a></body></html>"
    router.add("GET", "https://gizmo.test/", page(home))
    router.add("GET", "https://gizmo.test/build/api-reference", page(DOCS_PAGE))
    found, _ = await find_docs(BASE, client(router), net=PRIVATE)
    assert found is not None and found.found_by == "homepage"


async def test_follows_a_landing_page_one_click_to_the_reference(router: Router) -> None:
    home = "<html><body><a href='/gizmo-api'>Gizmo API</a></body></html>"
    landing = (
        "<html><body><a href='/gizmo-api/documentation'>Documentation</a>"
        "<a href='https://elsewhere.example/api'>Partner API</a></body></html>"
    )
    router.add("GET", "https://gizmo.test/", page(home))
    router.add("GET", "https://gizmo.test/gizmo-api", page(landing))
    # The index shows one example; the details are on its sub-pages.
    index = "<html><body><p>Try https://api.gizmo.test/v1/gizmos</p></body></html>"
    router.add("GET", "https://gizmo.test/gizmo-api/documentation", page(index))
    router.add("GET", "https://elsewhere.example/api", page(DOCS_PAGE * 5))
    found, _ = await find_docs(BASE, client(router), net=PRIVATE)
    assert found is not None and found.url == "https://gizmo.test/gizmo-api/documentation"
    assert not any("elsewhere" in str(r.url) for r in router.requests)  # official site only


async def test_a_page_with_one_example_is_not_docs_unless_it_says_so(router: Router) -> None:
    one = "<html><body><p>Try https://api.gizmo.test/v1/gizmos</p></body></html>"
    router.add("GET", "https://gizmo.test/developers", page(one))  # not a docs path
    found, _ = await find_docs(BASE, client(router), net=PRIVATE)
    assert found is None


async def test_a_linked_spec_wins(router: Router) -> None:
    router.add("GET", "https://api.gizmo.test/docs", lambda r: httpx.Response(200, json=SPEC))
    found, _ = await find_docs(BASE, client(router), net=PRIVATE)
    assert found is not None and found.spec is not None


async def test_search_prefers_official_and_skips_third_party(router: Router) -> None:
    router.add("GET", "https://gizmo-docs-mirror.example/api", page(DOCS_PAGE * 3))
    router.add("GET", "https://gizmo.readme.io/reference", page(DOCS_PAGE))
    router.add("GET", "https://gizmo.test/help/rest", page(DOCS_PAGE))
    queries: list[str] = []

    async def search(query: str) -> list[str]:
        queries.append(query)
        return [
            "https://gizmo-docs-mirror.example/api",  # looks best, but third-party
            "https://gizmo.readme.io/reference",
            "https://gizmo.test/help/rest",
        ]

    found, notes = await find_docs(BASE, client(router), name="Gizmo", searcher=search, net=PRIVATE)
    assert queries == ["Gizmo API documentation"]
    assert found is not None and found.url == "https://gizmo.test/help/rest"  # official first
    assert any("skipped 1 third-party" in n for n in notes)
    assert not any("mirror" in str(r.url) for r in router.requests)  # never even fetched

    found, _ = await find_docs(
        BASE,
        client(router),
        name="Gizmo",
        searcher=search,
        net=PRIVATE,
        allow_third_party=True,
        look_nearby=False,
    )
    assert found is not None and found.url == "https://gizmo.test/help/rest"  # still official


async def test_a_redirect_off_the_official_site_counts_as_third_party(router: Router) -> None:
    router.add(
        "GET",
        "https://docs.gizmo.test/",
        lambda r: httpx.Response(302, headers={"location": "https://elsewhere.example/docs"}),
    )
    router.add("GET", "https://elsewhere.example/docs", page(DOCS_PAGE))
    found, _ = await find_docs(BASE, client(router), net=PRIVATE)
    assert found is None


async def test_onboarding_with_only_a_base_url_finds_and_learns_the_docs(
    librarian: Librarian, router: Router
) -> None:
    router.add("GET", "https://docs.gizmo.test/", page(DOCS_PAGE))
    librarian.source_client_factory = lambda: client(router)

    def read(prompt: str) -> dict[str, Any]:
        assert "Page: https://docs.gizmo.test/" in prompt
        return {
            "operations": [
                {"method": "GET", "path": "/v1/gizmos", "evidence": "GET /v1/gizmos"},
                {"method": "GET", "path": "/v1/gizmos/{id}", "evidence": "GET /v1/gizmos/{id}"},
            ]
        }

    model, _ = extractor_model(read)
    librarian.extractor = build_extractor(model)
    outcome = await librarian.onboard("gizmo", base_url=BASE, name="Gizmo", caller=AGENT)
    source = outcome.entry.source
    assert (source.kind, source.url, source.found_by, source.trust) == (
        "docs",
        "https://docs.gizmo.test/",
        "well-known",
        "official",
    )
    assert "found by well-known (official)" in outcome.entry.notes
    assert outcome.read_operations == 2


async def test_search_is_used_only_when_configured(librarian: Librarian, router: Router) -> None:
    router.add(
        "GET",
        "https://searx.internal.test/search",
        lambda r: httpx.Response(200, json={"results": [{"url": "https://gizmo.test/help/rest"}]}),
    )
    router.add("GET", "https://gizmo.test/help/rest", page(DOCS_PAGE))
    librarian.source_client_factory = lambda: client(router)
    librarian.extractor = None
    librarian.settings.learning.model_knowledge = False
    with pytest.raises(LibrarianError, match="no documentation in the usual places"):
        await librarian.onboard("gizmo", base_url=BASE, caller=AGENT)
    assert not any("searx" in str(r.url) for r in router.requests)

    librarian.settings.learning.search = SearchSettings(
        provider="searxng", url="https://searx.internal.test"
    )
    with pytest.raises(LibrarianError, match="needs a model"):  # found it; reading needs a model
        await librarian.onboard("gizmo", base_url=BASE, caller=AGENT)
    assert any(str(r.url).startswith("https://searx.internal.test/search") for r in router.requests)


def test_gateway_can_use_the_anthropic_format(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)  # as in CI
    from pydantic_ai.models.anthropic import AnthropicModel

    from librarian.config import Settings
    from librarian.service import _model_from_settings  # pyright: ignore[reportPrivateUsage]

    model = _model_from_settings(
        Settings(
            model="claude-sonnet-5-5",
            model_base_url="http://litellm:4000/v1",
            model_api="anthropic",
        )
    )
    assert isinstance(model, AnthropicModel)
    assert model.model_name == "claude-sonnet-5-5"
    assert str(model.client.base_url).rstrip("/") == "http://litellm:4000"


async def test_model_search_refuses_to_pretend() -> None:
    from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from librarian.finddocs import SearchDidNotRun, model_searcher

    def from_memory(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart('{"urls": ["https://gizmo.test/docs"]}')])

    search = model_searcher(FunctionModel(from_memory), 5)
    with pytest.raises(SearchDidNotRun, match="needs an Anthropic model"):
        await search("Gizmo API documentation")


async def test_model_search_detects_a_silently_dropped_search() -> None:
    # A gateway can accept the search tool and drop it; the model then answers from memory.
    from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel
    from pydantic_ai.native_tools import WebSearchTool

    from librarian.finddocs import SearchDidNotRun, model_searcher

    def from_memory(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart('{"urls": ["https://gizmo.test/docs"]}')])

    claims_search = FunctionModel(
        from_memory, profile={"supported_native_tools": frozenset({WebSearchTool})}
    )
    with pytest.raises(SearchDidNotRun, match="answered without searching"):
        await model_searcher(claims_search, 5)("Gizmo API documentation")


@pytest.mark.parametrize("api", ["openai", "anthropic"])
def test_gateway_never_receives_a_provider_key_from_the_environment(
    api: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from librarian.config import Settings
    from librarian.service import NO_GATEWAY_KEY, _model_from_settings  # pyright: ignore[reportPrivateUsage]

    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
    settings = Settings(model="m", model_base_url="https://gateway.test/v1", model_api=api)  # pyright: ignore[reportArgumentType]
    model: Any = _model_from_settings(settings)
    assert model.client.api_key == NO_GATEWAY_KEY  # what the gateway would receive

    settings.model_api_key = SecretStr("sk-gateway")
    model = _model_from_settings(settings)
    assert model.client.api_key == "sk-gateway"
