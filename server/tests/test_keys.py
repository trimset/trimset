import itertools
import uuid

import pytest

from trimset_server.keys import (
    CAPABILITIES,
    PREFIXES,
    Principal,
    hash_key,
    looks_like_key,
    new_key,
)


@pytest.mark.req("FR-52")
@pytest.mark.parametrize("scope", list(PREFIXES))
def test_keys_carry_their_scope_prefix_and_32_base62_characters(scope: str) -> None:
    key = new_key(scope)  # type: ignore[arg-type]
    assert key.startswith(PREFIXES[scope])  # type: ignore[index]
    assert len(key) == 36
    assert looks_like_key(key)


def test_keys_are_not_repeated() -> None:
    assert len({new_key("ingest") for _ in range(1000)}) == 1000


@pytest.mark.parametrize(
    "text",
    [
        "",
        "tsx_" + "a" * 32,
        "tsa_" + "a" * 31,
        "tsa_" + "a" * 33,
        "tsa_" + "-" * 32,
        " tsa_" + "a" * 32,
    ],
)
def test_malformed_keys_are_not_keys(text: str) -> None:
    assert not looks_like_key(text)


def test_only_a_32_byte_hash_is_kept() -> None:
    key = new_key("admin")
    assert len(hash_key(key)) == 32
    assert hash_key(key) == hash_key(key)
    assert key.encode() not in hash_key(key)


# Every scope and capability, as in the table in docs/03-api/README.md.
EXPECTED = {
    ("proxy", "proxy"): True,
    ("ingest", "proxy"): True,
    ("ingest", "ingest"): True,
    ("review", "review"): True,
    ("admin", "proxy"): True,
    ("admin", "ingest"): True,
    ("admin", "review"): True,
    ("admin", "admin"): True,
}


@pytest.mark.req("FR-52")
@pytest.mark.parametrize(
    ("scope", "capability"),
    list(itertools.product(PREFIXES, ["proxy", "ingest", "review", "admin"])),
)
def test_scopes_allow_exactly_what_the_contract_says(scope: str, capability: str) -> None:
    principal = Principal(key_id=uuid.uuid4(), project_id=uuid.uuid4(), scope=scope)  # type: ignore[arg-type]
    assert principal.can(capability) is EXPECTED.get((scope, capability), False)
    assert set(CAPABILITIES) == set(PREFIXES)
