# Gate an agent's tool calls

An operations agent wants to run a database write or send an email. Should it run, wait
for a person, or not run at all? trimset answers with the same machinery it uses for any
decision: a decision model gives a probability, and the project's **approved policy**
turns it into an action, with a stated guarantee instead of a hunch.

The question is *"Is this tool call safe to run without a human?"*, and its probability is
that of **safe**. The policy says:

| Action | Means |
|---|---|
| `ACCEPT` | run it |
| `ESCALATE` | ask a person first |
| `BLOCK` | don't |

When trimset has nothing approved to go on, or anything fails, the answer is `ESCALATE`:
it asks instead of guessing, and it never says `ACCEPT` by default.

!!! note "Bad events are rare, so a guarantee takes evidence"
    A target such as "unsafe runs at most 0.5% of the time, with 95% confidence" needs
    about 600 labelled calls that ran with no incident. Until you have them, the compiler
    says "not enough labels: N more needed" and every call goes to a person. That is the
    honest answer, and the demo shows it.

## The MCP server

`trimset mcp` is a [Model Context Protocol](https://modelcontextprotocol.io) server over
standard input and output. It is part of the Python SDK (`pip install trimset`) and needs
no new endpoint on the server: its key is an ingest key, like any app's.

```json
{
  "mcpServers": {
    "trimset": {
      "command": "trimset",
      "args": ["mcp", "--model", "jev-latest"],
      "env": { "TRIMSET_URL": "https://trimset.example.com", "TRIMSET_KEY": "${TRIMSET_KEY}" }
    }
  }
}
```

Keep the key in your environment, not in a file you commit (Claude Code expands `${VAR}` in
this file; a client that doesn't needs the value in `env` directly). `--model` is the name your
decision model server knows (the same `model` your app sends through the proxy), and that
server must be an upstream of your trimset ([self-hosting](self-hosting.md)).

### Tools

| Tool | What it does |
|---|---|
| `gate` | Asks the decision model about `state` (what is about to run, e.g. `{"tool": "Bash", "input": {"command": "rm -rf build"}}`), then returns the policy's action, its reason, the probability and a `decision_id`. The decision is logged. |
| `outcome` | Reports what happened to an earlier decision: its `decision_id` and whether the call turned out `safe`. Recorded as a *downstream* outcome. |
| `explain` | Says what the active policy is: its rules in order, its stated guarantee, and, given a probability, what it would do with it. Read-only. |

### Setting up the question

The question's probability is that of *safe*, so tell trimset that a high value means
accept. Once the first call has created the question:

```bash
curl -X PATCH "$TRIMSET_URL/v1/admin/questions/safe_to_run" -H @- \
  -H 'content-type: application/json' \
  -d '{"settings": {"actions": {"high_means": "accept"}}}' <<< "Authorization: Bearer $ADMIN_KEY"
```

Then labels arrive as people approve or deny escalated calls, and you compile and approve
a policy as for any other question ([quickstart](quickstart.md)).

### What an agent can't do

- **Vouch for itself.** `gate` asks the decision model itself and ignores any probability the
  agent supplies. An operator can allow it with `trimset mcp --allow-probability`, for an
  agent that has already asked a model; the tool then accepts `probability` and
  `model_served`.
- **Write its own review.** `outcome` always records `downstream`, never a person's
  `human_review`, so an agent's report can't pose as the label that trains its own gate.
- **Be allowed by default.** A missing policy, a changed question, an unreachable model, a
  version the policy wasn't compiled for: each is `ESCALATE`.

### Advisory, not enforcing

An MCP tool is a thing an agent *may* call. If the agent doesn't, nothing stops the tool
call. Use `gate` where the agent is cooperative and you want its reasoning to include a
guarantee. Where a call must not run without the check, enforce it outside the agent's
control (a hook or a permission rule that runs before the tool does).

### Privacy

The tool call is sent to the decision model you configured, because that is what answers
the question. trimset keeps only a keyed hash of it. Long strings (a whole file being
written) are cut to 4,000 characters before they are sent.
