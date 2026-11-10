# Hashing and canonical forms

Every id and hash below has test vectors in [vectors/vectors.json](vectors/vectors.json).
Those vectors are produced by the reference script [vectors/generate.py](vectors/generate.py).
The server, the Python SDK and the TypeScript SDK (v0.2) must all reproduce them
exactly.

## 1. Canonical JSON

1. Apply **Unicode NFC** to every string and every object key, at any depth.
2. Serialise with **RFC 8785** (the JSON Canonicalization Scheme): object keys sorted,
   no whitespace, and numbers written the way ECMAScript writes them.

The result is UTF-8 bytes. Reference libraries: `rfc8785` 0.1.4 for Python and
`canonicalize` 5.1.0 for npm, both Apache-2.0.

## 2. Question definition and `question_id`

A question's definition (`question/1`, [schema](schemas/question.schema.json)) records
everything the model sees, in one form whatever the wire format
([ADR 0010](../adr/0010-question-id.md)):

| Field | From `/v1/systemone` (TypeSafe, Ollaya, Ollama) | From OpenAI `/v1/decisions` |
|---|---|---|
| `type` | `noul`, `choice` or `score` | `predicate` → `noul`; `choice`; `score` |
| `instructions` | `instructions`, as sent | `instructions`, as sent |
| …when absent or `null` | `{"$name": <the key of the question in "questions">}` | `{"$name": <"name">}` |
| `noul` | `{"true": criteria.true, "false": criteria.false}`, `null` where missing | `{"true": null, "false": null}` |
| `options` (choice) | `[{"value": key, "description": value}]` for each `criteria` entry, **in the order sent** | `[{"value": c.value, "description": c.description or null}]` for each of `choices`, in order |
| `levels` (score) | `[{"label": null, "description": d}]` for each `criteria` item | `[{"label": l.label, "description": l.description or null}]` for each of `levels` |

Notes:
- Choice options keep their order, because RFC 8785 would otherwise sort an object's keys.
- `"$name"` stands in when there are no instructions, because Ollaya shows the model the
  question's name in that case.

```
question_id = "q_" + first 32 hex characters of SHA-256( canonical(definition) )
```

A request's own key or name for a question is **not** hashed. It becomes the question's
default name.

### Test vectors

| Case | `question_id` |
|---|---|
| `noul_systemone` | `q_534a3508f2d81f8c35d11ba5cde4fcc3` |
| `noul_openai_same_question` (same question, OpenAI format) | `q_534a3508f2d81f8c35d11ba5cde4fcc3` |
| `noul_with_criteria` | `q_013f51a4dbae91a4f55d9a3f8da41752` |
| `noul_trailing_space_is_different` | `q_caf5f5cbc6b4b94eec8e693fa26af69a` |
| `noul_nfc_composed` (`café`, U+00E9) | `q_a379f5ca696049daaaac7347f26f8765` |
| `noul_nfc_decomposed_same_id` (`cafe` + U+0301) | `q_a379f5ca696049daaaac7347f26f8765` |
| `noul_no_instructions_uses_name` | `q_f71c16ff61fb209f14e28b2ded1ed493` |
| `choice_systemone` | `q_811c98e3cdebfb4bae6aee04cb3ab73f` |
| `choice_reordered_is_different` | `q_c8717eb3f321622064c2721c4f221f35` |
| `choice_openai_same_question` | `q_811c98e3cdebfb4bae6aee04cb3ab73f` |
| `choice_openai_boolean_value` (`true`) | `q_4f9f96190f24a2c9c98c18f6a3048808` |
| `choice_openai_string_value_is_different` (`"true"`) | `q_363e7298edda52b1b810006a3cef1137` |
| `score_systemone` | `q_a857099656a773e1fdd573d34c847b47` |
| `score_openai_with_labels_is_different` | `q_058d676d2caf1d9bf040bdcbeeaf6d20` |

The inputs, definitions and canonical bytes for each case are in `vectors.json`.

## 3. State hash

([ADR 0009](../adr/0009-privacy-model.md))

```
k_project  = HMAC-SHA-256( key = TRIMSET_SECRET, message = project_id )   # lowercase UUID text, UTF-8
state_hash = hex( HMAC-SHA-256( key = k_project, message = canonical(state) ) )
```

- **What "state" is:** `state` in `/v1/systemone`, and `input` in OpenAI's `/v1/decisions`.
- **Images:** systemone images (`assets`, multipart image parts) are not part of `state`,
  so they are neither hashed nor stored. OpenAI image parts sit inside `input`, so they
  are hashed as JSON.
- **The key in the bundle:** SDKs receive `k_project` in base64 in the policy bundle.

Test vectors, with server secret `trimset-test-secret-do-not-use` and project
`0193f0a2-7c4e-7b1a-9d2e-5f6a7b8c9d0e`, which give
`k_project = lBhyIw+OrsQrfIkq86dr5CJG8Atc9vVevtTkgk0G8rs=`:

| Case | Canonical state | `state_hash` |
|---|---|---|
| object | `{"amount":129.5,"reason":"Item never arrived","refund_id":"rf-1001"}` | `f4257bdbd8c5584fa33682e87a287739436d2831e7f9d95cea22230ed9586665` |
| same object, other key order | (same bytes) | `f4257bdbd8c5584fa33682e87a287739436d2831e7f9d95cea22230ed9586665` |
| string | `"Our checkout has returned 500 errors since 9am."` | `3a5438cfa08fa13b000b396adc183b7da44a2e4e025386ecf4bb529335e24315` |
| array | `[{"content":"Where is my order?","role":"user"}]` | `3d01b555cf3677a7162eda92ac4fea6a65a8efd0edc27b96db2b85cecb45c053` |

## 4. Policy fingerprint

([ADR 0008](../adr/0008-policy-distribution.md))

```
fingerprint = "sha256:" + hex( SHA-256( canonical(policy without "fingerprint") ) )
```

[examples/policy.json](examples/policy.json) has the fingerprint
`sha256:32d8f6efeee249f92884e5bf3067e1b38df4ea9ec5bc1f4c088c97220bb0e94c`.

## 5. Bundle ETag

```
etag = hex( SHA-256( canonical(bundle without "generated_at" and "etag") ) )
```

The HTTP `ETag` header carries the same value in double quotes. Any change to a policy,
a question name or the state-hash key changes it.

## Checking

```bash
uv run --no-project --with rfc8785==0.1.4 python docs/03-api/vectors/generate.py --check
```

The check verifies four things:
- the vectors are current;
- the pairs that must share an id do, and the pairs that must differ do;
- key order doesn't change a state hash;
- the examples' fingerprint and ETag are right.
