"""Canonical JSON, question ids, state hashes, policy fingerprints and bundle ETags.

The specification is docs/03-api/hashing.md. The tests reproduce every value in
docs/03-api/vectors/vectors.json, which the TypeScript SDK must match too.
"""

import hashlib
import hmac
import unicodedata
from collections.abc import Mapping
from typing import Any

import rfc8785


def _nfc(value: Any) -> Any:
    """Unicode NFC on every string and object key, at any depth."""
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, list):
        return [_nfc(v) for v in value]
    if isinstance(value, Mapping):
        return {_nfc(k): _nfc(v) for k, v in value.items()}
    return value


def canonical(value: Any) -> bytes:
    """NFC, then RFC 8785. Raises rfc8785's errors for values JSON can't carry exactly
    (NaN, infinities, integers beyond 2**53)."""
    return rfc8785.dumps(_nfc(value))


def question_id(definition: Mapping[str, Any]) -> str:
    """`q_` and the first 128 bits of the SHA-256 of a question/1 definition (ADR 0010)."""
    return "q_" + hashlib.sha256(canonical(definition)).hexdigest()[:32]


def project_state_key(server_secret: bytes, project_id: str) -> bytes:
    """The per-project key for state hashes, derived from the server secret (ADR 0009)."""
    return hmac.new(server_secret, str(project_id).lower().encode("utf-8"), hashlib.sha256).digest()


def state_hash(key: bytes, state: Any) -> str:
    """HMAC-SHA-256 of the canonical state. The state itself is never stored."""
    return hmac.new(key, canonical(state), hashlib.sha256).hexdigest()


def fingerprint(policy: Mapping[str, Any]) -> str:
    """`sha256:` and the SHA-256 of the canonical policy without its fingerprint (ADR 0008)."""
    body = {k: v for k, v in policy.items() if k != "fingerprint"}
    return "sha256:" + hashlib.sha256(canonical(body)).hexdigest()


def bundle_etag(bundle: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical bundle without `generated_at` and `etag`."""
    body = {k: v for k, v in bundle.items() if k not in ("generated_at", "etag")}
    return hashlib.sha256(canonical(body)).hexdigest()
