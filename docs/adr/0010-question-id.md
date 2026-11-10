# 0010. `question_id` hashing

**Status:** proposed (Phase 2, 2026-11-10) · **Requirements:** FR-03, FR-15, FR-31

## Context

Fits and policies must never be pooled across wordings (FR-15). The brief defines
`question_id` as a stable hash of the exact wording and the option order. People still
want readable names such as `refund_fraud`.

## Options

1. **Hash only the text.** Misses changes to the options, their order, or the type.
2. **Hash a normalised text** (lowercased, whitespace collapsed). Two wordings the model
   sees differently would collide.
3. **Hash everything the model sees, exactly, after Unicode NFC normalisation.**

## Decision

Option 3.

- **What is hashed:** the object `{"v": 1, "type": …, "text": …, "options": […]}` for
  choice, or `"levels": […]` for score. It also includes every other field of the
  question that the model sees, such as option descriptions. Phase 3 fixes that list
  once the wire format is confirmed.
- **What is not hashed:** the request's own local key or name for the question, and
  anything the model does not see.
- **Normalisation:** Unicode NFC only. No trimming and no case folding: "exact wording"
  means exact.
- **The id:** `question_id = "q_" +` the first 32 hex characters (128 bits) of the
  SHA-256 of the object's RFC 8785 canonical JSON.

**Names:**
- A project attaches a stable name to an id. In proxy mode, the wire format's own key for
  the question, if it has one, becomes the default name.
- A name points to its latest id, and old ids keep their history.
- Policies never move between ids.

**Gating by name:**
- `gate("refund_fraud", …)` looks up the name's id in the bundle.
- If the app also passes the question object, the SDK computes its id. If the id differs,
  the SDK returns the default with reason `question_changed`.

## Consequences

- Changing one word starts a new question, with no history and no policy, which is
  intended. The CLI shows the new question next to its predecessor.
- Phase 3 publishes test vectors, so the TypeScript SDK computes the same ids.
