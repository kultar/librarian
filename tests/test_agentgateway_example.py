"""The agentgateway example (examples/agentgateway) is what someone runs to see librarian
behind their gateway. It claims to be the configuration the docs give and the weekly
integration run tests, on the quick start's settings: a test keeps those the same, so that
what is tried is what is tested."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

import librarian
from librarian.config import Settings
from librarian.registry.store import load_entry
from librarian.secrets import EnvSecretStore

ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / "examples" / "agentgateway"
QUICKSTART = ROOT / "examples" / "quickstart"
TESTED = ROOT / "tests" / "integration" / "agentgateway"
GUIDE = (EXAMPLE / "README.md").read_text("utf-8")
COMPOSE: dict[str, Any] = yaml.safe_load((EXAMPLE / "docker-compose.yaml").read_text("utf-8"))
GATEWAY: dict[str, Any] = yaml.safe_load((EXAMPLE / "agentgateway.yaml").read_text("utf-8"))
SERVICES: dict[str, Any] = COMPOSE["services"]


def published(service: str) -> int:
    """The port of a service on the machine that runs the example."""
    (mapping,) = SERVICES[service]["ports"]
    return int(mapping.split(":")[0])


def test_the_gateway_is_set_up_as_the_docs_say_and_as_the_weekly_run_tests() -> None:
    docs = (ROOT / "docs" / "integrations.md").read_text("utf-8")
    shown = re.search(
        r"### librarian authenticates: each agent's key passes through.*?```yaml\n(.*?)```",
        docs,
        flags=re.S,
    )
    assert shown is not None
    assert yaml.safe_load(shown.group(1)) == GATEWAY
    # The weekly run has a second way of using the gateway beside it; this is its first.
    tested = yaml.safe_load((TESTED / "agentgateway.yaml").read_text("utf-8"))
    assert tested["binds"][0] == GATEWAY["binds"][0]
    stack = yaml.safe_load((TESTED / "docker-compose.yaml").read_text("utf-8"))
    assert SERVICES["agentgateway"]["image"] == stack["services"]["agentgateway"]["image"]
    assert SERVICES["agentgateway"]["image"].rsplit(":", 1)[1] in GUIDE  # the version it names


def test_the_example_runs_the_release_it_names_on_the_quick_starts_settings() -> None:
    version = librarian.__version__
    assert SERVICES["librarian"]["image"] == f"ghcr.io/kultar/librarian:{version}"
    assert f"librarian {version} as published" in GUIDE
    mounted = {volume.split(":")[0] for volume in SERVICES["librarian"]["volumes"]}
    assert {"../quickstart/librarian.yaml", "../quickstart/registry"} <= mounted
    settings = Settings.load(QUICKSTART / "librarian.yaml")
    entry = load_entry(QUICKSTART / "registry" / "demo.yaml")

    # The keys: the agent's (the one the `ask` helper sends unless told otherwise) and the
    # demo API's two, each under the name librarian looks for.
    given: dict[str, str] = SERVICES["librarian"]["environment"]
    names = EnvSecretStore()
    (caller,) = settings.callers
    assert caller.key_ref is not None
    key = given[names.env_name(caller.key_ref)]
    assert f'KEY="${{LIBRARIAN_KEY:-{key}}}"' in (QUICKSTART / "ask").read_text("utf-8")
    assert f"Authorization: Bearer {key}" in GUIDE
    for ref in (entry.secret_ref, entry.write_secret_ref):
        assert ref is not None and names.env_name(ref) in given

    # The demo API is the service its entry names.
    host, port = entry.base_url.removeprefix("http://").split(":")
    assert host in SERVICES and port == "8080"


def test_agents_are_given_the_gateway_and_people_librarians_pages() -> None:
    gateway = published("agentgateway")
    assert GATEWAY["binds"][0]["port"] == gateway
    assert f"export LIBRARIAN_URL=http://localhost:{gateway}" in GUIDE
    assert f"http://localhost:{gateway}/mcp" in GUIDE
    for route in GATEWAY["binds"][0]["listeners"][0]["routes"]:
        (backend,) = route["backends"]
        target = (
            backend["mcp"]["targets"][0]["mcp"]["host"] if "mcp" in backend else backend["host"]
        )
        assert target.removeprefix("http://").split("/")[0] == "librarian:8080"
    # Approval links are made from the quick start's public_url: librarian's own port.
    settings = Settings.load(QUICKSTART / "librarian.yaml")
    assert settings.public_url == f"http://localhost:{published('librarian')}"
    assert f"http://localhost:{published('librarian')}/approvals/" in GUIDE
