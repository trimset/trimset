"""Property tests: canonical JSON ignores key order and Unicode composition."""

import unicodedata
from typing import Any

import pytest

hypothesis = pytest.importorskip("hypothesis")  # not installed in the Python 3.11 job
from hypothesis import given  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from trimset.hashing import canonical, question_id  # noqa: E402

scalars = (
    st.none()
    | st.booleans()
    | st.integers(min_value=-(2**53) + 1, max_value=2**53 - 1)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=12)
)
json_values = st.recursive(
    scalars,
    lambda inner: (
        st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=6), inner, max_size=4)
    ),
    max_leaves=12,
)


def _reverse_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _reverse_keys(value[k]) for k in reversed(list(value))}
    if isinstance(value, list):
        return [_reverse_keys(v) for v in value]
    return value


def _decompose(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFD", value)
    if isinstance(value, dict):
        return {_decompose(k): _decompose(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decompose(v) for v in value]
    return value


@pytest.mark.req("FR-03")
@given(json_values)
def test_key_order_never_changes_the_bytes(value: Any) -> None:
    assert canonical(value) == canonical(_reverse_keys(value))


@pytest.mark.req("FR-03")
@given(st.text(min_size=1, max_size=30))
def test_unicode_composition_never_changes_the_question_id(text: str) -> None:
    question = {
        "schema": "question/1",
        "type": "noul",
        "instructions": text,
        "noul": {"true": None, "false": None},
    }
    assert question_id(question) == question_id(_decompose(question))
