"""What `capabilities` answers once there is more than fits in an agent's context.

It is the first thing an agent asks, and the whole answer goes into its context. With a
few large APIs it used to be tens of thousands of tokens, which agent clients warn
about, put aside in a file, or refuse. So it has a size it stays under: the lists of
operations are cut, every API is still named, and the answer says how to see the rest.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from librarian.audit import MemoryAuditSink
from librarian.calls import Caller
from librarian.capabilities import FIRST, MATCHES, SHORTER, Listed, shortened, size
from librarian.config import MIN_CAPABILITIES_BYTES, Settings
from librarian.executor import PolicyDenied
from librarian.secrets import FileSecretStore
from librarian.service import Librarian
from tests.conftest import RecordingNotifier, Router, write_entries

EVERYTHING = Caller(agent="agent-a", write_apis=("*",))
LIMIT = Settings().capabilities_max_bytes


# Sixty words to tell operations apart by (operation search leaves numbers out).
KINDS = [
    colour + animal
    for colour in ("red", "blue", "gold", "grey", "pink", "teal")
    for animal in ("ant", "bat", "cod", "dog", "elk", "fox", "gnu", "hen", "jay", "owl")
]


def api(name: str, reads: int, writes: int = 0, about: str = "things") -> dict[str, Any]:
    """An API with that many operations that read and that write."""
    return {
        "id": name,
        "name": f"{name.title()} API",
        "description": f"The {name} service: {about} records and what belongs to them.",
        "base_url": f"https://{name}.example.test/v1",
        "status": "verified",
        "auth": {"type": "bearer", "secret_ref": f"{name}/readonly"},
        "write_auth": {"type": "bearer", "secret_ref": f"{name}/write"},
        "operations": [
            {
                "id": f"{name}Get{kind.title()}",
                "method": "GET",
                "path": f"/{kind}/{{id}}",
                "summary": f"Get the {kind} {about} records by id",
                "access": "read",
                "parameters": [{"name": "id", "in": "path"}, {"name": "fields", "in": "query"}],
            }
            for kind in KINDS[:reads]
        ]
        + [
            {
                "id": f"{name}Set{kind.title()}",
                "method": "POST",
                "path": f"/{kind}/{{id}}",
                "summary": f"Change the {kind} {about} records",
                "access": "write",
                "writable": True,
                "accepts_body": True,
                "parameters": [{"name": "id", "in": "path"}],
            }
            for kind in KINDS[:writes]
        ],
    }


LARGE = [
    api(name, 60, 60, about)
    for name, about in (
        ("alpha", "invoices"),
        ("bravo", "tickets"),
        ("charlie", "repositories"),
        ("delta", "customers"),
        ("echo", "shipments"),
        ("foxtrot", "invoices"),
        ("golf", "calendars"),
        ("hotel", "bookings"),
    )
]
SMALL = api("zulu", 3, 0, "notes")


def serving(settings: Settings, *entries: dict[str, Any]) -> Librarian:
    assert settings.secrets_dir is not None
    write_entries(settings.registry_dir, *entries)
    lib = Librarian(
        settings,
        secrets_store=FileSecretStore(settings.secrets_dir),
        audit=MemoryAuditSink(),
        notifier=RecordingNotifier(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(Router())),
    )
    lib.load()
    return lib


def lists(answer: dict[str, Any]) -> dict[str, tuple[int, int]]:
    """How many operations each API is listed with: (reads, writes)."""
    return {
        a["api"]: (len(a["read_operations"]), len(a.get("write_operations", [])))
        for a in answer["apis"]
    }


def test_an_answer_that_fits_is_what_it_always_was(settings: Settings) -> None:
    """Nothing changes for a registry whose answer is small: no note, the first 50 of
    each kind without words, the 20 that fit best with them."""
    lib = serving(settings, LARGE[0], SMALL)
    answer = lib.capabilities(None, EVERYTHING)
    assert list(answer) == ["apis"]
    assert lists(answer) == {"alpha": (FIRST, FIRST), "zulu": (3, 0)}
    assert size(answer) <= LIMIT
    alpha = answer["apis"][0]
    assert list(alpha) == [
        "api", "name", "description", "status", "base_url", "read_operations",
        "total_read_operations", "write_operations", "total_write_operations",
        "credential_ready", "learned_from",
    ]  # fmt: skip
    assert alpha["total_read_operations"] == 60 and alpha["total_write_operations"] == 60
    found = lib.capabilities("alpha invoices", EVERYTHING)
    assert "note" not in found and lists(found)["alpha"] == (MATCHES, MATCHES)


def test_every_api_is_named_and_the_lists_are_cut_equally_until_it_fits(
    settings: Settings,
) -> None:
    """Eight large APIs made an answer of 90 KB. Now every one of them is still there
    with how many operations it has, their lists are cut to the same length, a small
    API keeps all it has, and the answer says what happened and how to see the rest."""
    lib = serving(settings, *LARGE, SMALL)
    lib.settings.capabilities_max_bytes = 0
    assert size(lib.capabilities(None, EVERYTHING)) > 80_000  # what it was
    lib.settings.capabilities_max_bytes = LIMIT

    answer = lib.capabilities(None, EVERYTHING)
    assert size(answer) <= LIMIT
    shown = lists(answer)
    assert list(shown) == [e["id"] for e in LARGE] + ["zulu"]  # all of them, in order
    assert shown.pop("zulu") == (3, 0)  # nothing to cut there
    [(most, same)] = set(shown.values())
    assert most == same and most in SHORTER and 0 < most < FIRST
    for listed in answer["apis"][:-1]:
        assert listed["total_read_operations"] == 60 and listed["total_write_operations"] == 60
        assert [op["operation"] for op in listed["read_operations"]] == [
            f"{listed['api']}Get{kind.title()}" for kind in KINDS[:most]
        ]  # the first ones, as before
    assert answer["note"] == (
        f"Shortened to keep this answer small: each list of operations holds at most {most} "
        "(total_read_operations and total_write_operations say how many there are). Call "
        "capabilities with api (an id from this answer) to see one API's operations, and add "
        "a query, a few words for what you need, to get the ones that fit."
    )
    # No shorter than it has to be: the next length up would have been over the limit.
    longer = SHORTER[SHORTER.index(most) - 1]
    lib.settings.capabilities_max_bytes = 0
    next_up = lib.capabilities(
        None, EVERYTHING, fits=lambda a: max(lists(a).values()) <= (longer, longer)
    )
    assert max(lists(next_up).values()) == (longer, longer) and size(next_up) > LIMIT


def test_with_words_the_api_that_fits_them_best_keeps_its_matches(settings: Settings) -> None:
    """An agent that names the API it wants gets that one's operations as usual, however
    many other APIs its words also touch: those are the ones that are cut."""
    lib = serving(settings, *LARGE, SMALL)
    answer = lib.capabilities("foxtrot tealowl records", EVERYTHING)
    assert size(answer) <= LIMIT
    shown = lists(answer)
    assert next(iter(shown)) == "foxtrot" and len(shown) > 2
    assert shown.pop("foxtrot") == (MATCHES, MATCHES)
    shown.pop("zulu", None)  # the small one has less than the others are cut to
    [(most, same)] = set(shown.values())
    assert most == same and most < MATCHES
    best = answer["apis"][0]["read_operations"][0]
    assert best["operation"] == "foxtrotGetTealowl"  # still ranked for the words
    assert answer["note"].startswith(
        "Shortened to keep this answer small: the operations of 'foxtrot', which fits your "
        "words best, are listed as usual; "
        + (
            f"every other list of operations holds at most {most} "
            if most
            else "the other APIs are named without their operations "
        )
    )
    assert answer["note"].endswith("to get the ones that fit.")


def test_one_api_can_be_asked_for_and_then_it_is_the_only_one(settings: Settings) -> None:
    """What the note points to: `api` gives that API alone, so its lists have the room."""
    lib = serving(settings, *LARGE, SMALL)
    alone = lib.capabilities(None, EVERYTHING, api="delta")
    assert "note" not in alone and lists(alone) == {"delta": (FIRST, FIRST)}
    fitting = lib.capabilities("goldfox customers", EVERYTHING, api="delta")
    assert "note" not in fitting and lists(fitting) == {"delta": (MATCHES, MATCHES)}
    assert fitting["apis"][0]["read_operations"][0]["operation"] == "deltaGetGoldfox"
    # Words that fit none of its operations: they are listed all the same (as since 0.22.1).
    assert lists(lib.capabilities("zebra", EVERYTHING, api="delta")) == {"delta": (20, 20)}

    # One API can be too much by itself: then it is cut too, and the way on is a query.
    lib.settings.capabilities_max_bytes = 4_000
    cut = lib.capabilities(None, EVERYTHING, api="delta")
    [(most, _)] = lists(cut).values()
    assert size(cut) <= 4_000 and 0 < most < FIRST
    assert cut["note"] == (
        f"Shortened to keep this answer small: each list of operations holds at most {most} "
        "(total_read_operations and total_write_operations say how many there are). Give a "
        "query, a few words for what you need, to get the ones that fit."
    )


def test_asking_for_an_api_that_is_not_there_is_refused_in_words(settings: Settings) -> None:
    """...and one written another way is answered with the one that exists, but only
    among the APIs that caller may use: nothing else is given away."""
    blocked = {**api("yankee", 1), "status": "blocked"}
    lib = serving(settings, *LARGE[:3], blocked)

    def refused(name: str, caller: Caller = EVERYTHING) -> str:
        with pytest.raises(PolicyDenied) as denied:
            lib.capabilities(None, caller, api=name)
        return denied.value.reason

    assert refused("nope") == "unknown API 'nope'"
    assert refused("Alpha") == "unknown API 'Alpha'. Did you mean 'alpha'?"
    assert refused("yankee") == "API 'yankee' is blocked"
    assert refused("YANKEE") == "unknown API 'YANKEE'"  # a blocked one is not suggested
    only_alpha = Caller(agent="narrow", apis=("alpha",))
    assert refused("bravo", only_alpha) == "caller 'narrow' is not allowed to use API 'bravo'"
    assert refused("Bravo", only_alpha) == "caller 'narrow' is not allowed to use API 'Bravo'"
    assert refused("ALPHA", only_alpha) == (
        "caller 'narrow' is not allowed to use API 'ALPHA'. Did you mean 'alpha'?"
    )
    assert lists(lib.capabilities(None, only_alpha, api="alpha")) == {"alpha": (FIRST, 0)}


def test_more_apis_than_fit_are_still_all_named(settings: Settings) -> None:
    """Past a few dozen APIs not even their descriptions fit. The first ones are given
    as usual, without operations, and every other one by its id: an agent can ask about
    any of them, and none is there without the agent being told."""
    many = [api(f"service{n:03}", 2, 0, f"department {n}") for n in range(150)]
    lib = serving(settings, *many)
    answer = lib.capabilities(None, EVERYTHING)
    assert size(answer) <= LIMIT
    listed = [a["api"] for a in answer["apis"]]
    assert 20 < len(listed) < 150
    assert [*listed, *answer["more_apis"]] == [f"service{n:03}" for n in range(150)]
    for told in answer["apis"]:
        assert told["read_operations"] == [] and told["total_read_operations"] == 2
    assert answer["note"] == (
        f"Shortened to keep this answer small: {len(listed)} of the 150 APIs are listed, "
        "without their operations; more_apis has the ids of the others. Call capabilities "
        "with api (an id from this answer) to see one API's operations, and add a query, a "
        "few words for what you need, to get the ones that fit."
    )
    # As many in full as fit, not fewer: one more would not have.
    one_more = lib.capabilities(None, EVERYTHING, api=answer["more_apis"][0])["apis"][0]
    one_more["read_operations"] = []
    more = {**answer, "apis": [*answer["apis"], one_more], "more_apis": answer["more_apis"][1:]}
    assert size(more) > LIMIT
    # ...and what the note says works, for one that was only named.
    last = lib.capabilities(None, EVERYTHING, api="service149")
    assert "note" not in last and len(last["apis"][0]["read_operations"]) == 2
    found = lib.capabilities("service149", EVERYTHING)
    assert found["apis"][0]["api"] == "service149"


def test_the_limit_can_be_lifted_and_cannot_be_set_to_nothing(settings: Settings) -> None:
    lib = serving(settings, *LARGE)
    lib.settings.capabilities_max_bytes = 0
    whole = lib.capabilities(None, EVERYTHING)
    assert "note" not in whole and set(lists(whole).values()) == {(FIRST, FIRST)}
    # A caller's own bound is kept with no limit set (the MCP endpoint passes one).
    held = lib.capabilities(None, EVERYTHING, fits=lambda answer: size(answer) <= 30_000)
    assert size(held) <= 30_000 and "note" in held and len(held["apis"]) == len(LARGE)

    assert Settings(capabilities_max_bytes=0).capabilities_max_bytes == 0
    assert Settings(capabilities_max_bytes=MIN_CAPABILITIES_BYTES).capabilities_max_bytes == 2_000
    with pytest.raises(ValidationError, match="leaves no room to name even a few APIs"):
        Settings(capabilities_max_bytes=MIN_CAPABILITIES_BYTES - 1)


def listed(name: str, reads: int, writes: int | None = None) -> Listed:
    def ops(kind: str, count: int) -> list[dict[str, Any]]:
        return [{"operation": f"{name}{kind}{n:02}", "summary": "x" * 40} for n in range(count)]

    return Listed(
        id=name,
        head={"api": name, "description": "d" * 100},
        reads=ops("Get", reads),
        total_reads=reads * 3,
        writes=None if writes is None else ops("Set", writes),
        total_writes=(writes or 0) * 3,
        tail={"learned_from": "openapi"},
    )


@pytest.mark.parametrize("searched", [False, True])
@pytest.mark.parametrize("limit", [5, 300, 900, 2_000, 4_000, 7_000, 12_000, 20_000, 40_000])
def test_the_answer_is_the_least_shortened_one_that_fits(limit: int, searched: bool) -> None:
    """Whatever the room: the answer fits where anything can, and the step before the one
    it took would not have."""
    apis = [listed("a", 50, 50), listed("b", 50, 50), listed("c", 40), listed("d", 4, 2)]

    def fits(answer: dict[str, Any]) -> bool:
        return size({"apis": answer["apis"]}) <= limit  # the note aside: it varies a little

    answer = shortened(apis, fits, searched=searched, one=False)
    whole = {"apis": [a.told(FIRST) for a in apis]}
    if fits(whole):
        assert answer == whole
        return
    assert "note" in answer
    took = lists(answer)

    def plan(first: int, rest: int, count: int = 4) -> dict[str, tuple[int, int]]:
        made = {"apis": [a.told(first if n == 0 else rest) for n, a in enumerate(apis[:count])]}
        return lists(made)

    # Every step there is, from the least shortened to the most.
    steps = [plan(FIRST, FIRST)]
    if searched:
        steps += [plan(FIRST, most) for most in SHORTER]
    steps += [plan(most, most) for most in SHORTER]
    steps += [plan(0, 0, count) for count in (3, 2, 1, 0)]
    steps = [step for n, step in enumerate(steps) if step not in steps[:n]]
    assert took in steps
    if not fits(answer):  # nothing fits: the least there is, which is the last step
        assert took == steps[-1] == {}
        return
    before = steps[steps.index(took) - 1]
    less_cut = {"apis": [a.told(before[a.id][0]) for a in apis if a.id in before]}
    assert lists(less_cut) == before
    assert not fits(less_cut), "a less shortened answer would have fitted"


def test_nothing_is_tried_that_would_change_nothing() -> None:
    """Lists already shorter than a step are not cut to it: the answer is weighed once
    for each step that differs, however large the registry."""
    apis = [listed(name, 4, 4) for name in "abcdefgh"]
    weighed: list[dict[str, Any]] = []

    def never(answer: dict[str, Any]) -> bool:
        weighed.append(answer)
        return False

    shortened(apis, never, searched=False, one=False)
    seen = [lists(answer) for answer in weighed]
    assert all(step not in seen[:n] for n, step in enumerate(seen))
    assert len(weighed) <= 1 + 3 + 4  # whole; cut to 3, 1, 0; then fewer APIs, by halving
