"""The kagent example (examples/kagent) is followed command by command by someone who has
never run librarian. No automated run starts a cluster for it, so what can be checked
without one is checked here: that its files fit each other, the chart and the release
they name. A mismatch would otherwise show as an agent that is refused, an API that does
not answer or a chart that is not found, in front of a newcomer."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

import librarian
from librarian.registry import Access
from librarian.registry.store import load_entry
from librarian.secrets import EnvSecretStore

ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / "examples" / "kagent"
GUIDE = (EXAMPLE / "README.md").read_text("utf-8")
VALUES: dict[str, Any] = yaml.safe_load((EXAMPLE / "values.yaml").read_text("utf-8"))
CHART: dict[str, Any] = yaml.safe_load(
    (ROOT / "deploy" / "helm" / "librarian" / "values.yaml").read_text("utf-8")
)
# The namespace the guide installs librarian into.
NAMESPACE = re.search(r"--namespace (\S+) --create-namespace", GUIDE)


def manifests(kind: str) -> list[dict[str, Any]]:
    documents = yaml.safe_load_all((EXAMPLE / "demo.yaml").read_text("utf-8"))
    return [document for document in documents if document and document["kind"] == kind]


def test_the_agent_is_the_one_the_rule_admits_and_names_the_server_the_chart_registers() -> None:
    assert NAMESPACE is not None
    (agent,) = manifests("Agent")
    (rule,) = VALUES["substrate"]["rules"]
    assert rule["match"] == {
        "namespace": agent["metadata"]["namespace"],
        "name": agent["metadata"]["name"],
    }
    # The Agent finds librarian's tools under the name and namespace the chart gives the
    # RemoteMCPServer: its default name, in the release's namespace.
    (binding,) = agent["spec"]["template"]["inline"]["tools"]
    registered = CHART["substrate"]["kagent"]["remoteMCPServer"]
    assert registered["create"] is True and not registered["namespace"]
    assert VALUES["substrate"]["enabled"] is True and "kagent" not in VALUES["substrate"]
    assert binding["mcp"]["server"] == {
        "kind": "RemoteMCPServer",
        "name": registered["name"],
        "namespace": NAMESPACE.group(1),
    }
    # The helper asks this Agent unless told otherwise.
    assert f'agent="${{AGENT:-{agent["metadata"]["name"]}}}"' in (EXAMPLE / "ask").read_text()


def test_the_demo_api_is_where_its_entry_says_and_librarian_has_its_keys() -> None:
    assert NAMESPACE is not None
    sync = VALUES["registry"]["gitSync"]
    entry = load_entry(ROOT / sync["path"] / "demo.yaml")
    (rule,) = VALUES["substrate"]["rules"]
    assert rule["write_apis"] == [entry.id]
    assert any(op.access is Access.WRITE and op.writable for op in entry.operations)

    # The entry names the API by its Service's short name, which resolves only in the
    # namespace librarian runs in; and it is inside the cluster, which has to be allowed.
    (service,) = manifests("Service")
    (port,) = service["spec"]["ports"]
    assert entry.base_url == f"http://{service['metadata']['name']}:{port['port']}"
    assert service["metadata"]["namespace"] == NAMESPACE.group(1)
    (deployment,) = manifests("Deployment")
    assert deployment["metadata"]["namespace"] == service["metadata"]["namespace"]
    assert deployment["spec"]["template"]["metadata"]["labels"] == service["spec"]["selector"]
    assert VALUES["config"]["allow_private_networks"] is True

    given = {variable["name"] for variable in VALUES["env"]}
    names = EnvSecretStore()
    for ref in (entry.secret_ref, entry.write_secret_ref):
        assert ref is not None
        assert names.env_name(ref) in given


def test_the_example_names_the_release_it_is_run_with() -> None:
    """The chart, the image it brings and the entry of the demo API are of one release: an
    entry from a later one may hold what the image cannot read yet."""
    version = librarian.__version__
    assert f"oci://ghcr.io/kultar/charts/librarian --version {version} " in GUIDE
    sync = VALUES["registry"]["gitSync"]
    assert sync["enabled"] is True and sync["ref"] == f"v{version}"
    assert sync["repo"] == "https://github.com/kultar/librarian.git"
    assert "image" not in VALUES  # the image is the chart's own


def test_the_skill_is_named_the_only_way_kagent_takes_one() -> None:
    """kagent 1.0 takes an image by digest and nothing that can move (a tag is refused),
    and the folder has to be in the image under the path given."""
    (agent,) = manifests("Agent")
    (skill,) = agent["spec"]["template"]["inline"]["skills"]
    assert re.fullmatch(r"ghcr\.io/kultar/librarian@sha256:[0-9a-f]{64}", skill["source"]["oci"])
    path = skill["source"]["path"]
    assert (ROOT / path / "SKILL.md").is_file()
    folder = path.split("/")[0]
    assert f"COPY {folder} /{folder}" in (ROOT / "Dockerfile").read_text("utf-8")


def test_the_guide_asks_the_way_the_helper_and_the_kagent_cli_expect() -> None:
    """The kagent CLI talks to localhost:8083 unless told otherwise, and approval links
    are made from `publicUrl`: the guide's two forwards have to be those two."""
    assert "port-forward svc/kagent-controller 8083:8083" in GUIDE
    pages = re.fullmatch(r"http://localhost:(\d+)", VALUES["publicUrl"])
    assert pages is not None
    assert f"port-forward svc/librarian {pages.group(1)}:{pages.group(1)}" in GUIDE
    assert f"<{VALUES['publicUrl']}>" in GUIDE
    # The cluster the guide makes is the one it removes.
    made = re.search(r"kind create cluster --name (\S+) ", GUIDE)
    assert made is not None
    assert f"kind delete cluster --name {made.group(1)}" in GUIDE
