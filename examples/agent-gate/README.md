# Agent gate demo: is this tool call safe to run without a human?

Gating an AI agent's tool calls ([UC-5](../../docs/01-requirements.md#uc-5-agent-tool-approval)).
Before an agent runs a command, a decision model is asked "Is this tool call safe to run
without a human?". trimset's approved policy turns its answer into *run it*, *ask a person
first* or *don't*. This demo takes a project from nothing to Claude Code's `PreToolUse`
hook and the MCP server both obeying a certified policy.

```bash
examples/agent-gate/run.sh
```

It needs Docker, curl, openssl and python3, and takes a few minutes, most of it the first
build. It writes a `.env` with a random database password and secret if you don't have one.

## What it does

1. Starts trimset and the stand-in decision model, and creates a project with an admin key
   (the approver) and an ingest key (all the agent holds: it can send decisions and read
   policies, never approve one).
2. **Day one.** Runs the hook on a call before there is any history. There is no approved
   policy, so the answer is `ask`: trimset won't guess.
3. Loads two months of tool calls (12,000), with what happened later: a person's review of
   the ones the model escalated, an incident report for an unsafe call that ran, and a rule
   that counts a call with no incident in three weeks as safe.
4. `trimset calibrate`, then `trimset policy compile`: first the target the use case asks
   for (at most 0.5% of the calls it lets through may be unsafe, with 95% confidence), and
   if two months of labels can't certify that, a looser 2%. The compiler says so when it
   can't; it doesn't hand over a policy it can't stand behind.
5. `submit`, `approve` and `activate`, each with a reason on the record.
6. Runs the hook on nine calls: tests, `pwd`, a diff and an edit, then a package install,
   a Terraform apply, a `DELETE`, a force push and `rm -rf /`.
7. Shows `--grant`, which lets a call the policy accepts run without a prompt. It is off by
   default.
8. Points the hook at a trimset that isn't there. The answer is still `ask`.
9. Drives `trimset mcp` over its protocol: lists its tools, gates a call, has the policy
   explained, then reports that the call turned out to break something (`outcome`), which
   the next refit learns from.

## What you should see

- Day one: `ask`, "there is no approved policy for this question yet".
- A policy for the question (an accept rule and the guarantee it was certified for), then
  nine hook decisions. The hook only makes Claude Code stricter by default, so each call is
  either `ask` (a person decides) or `silent` (trimset has no objection, and Claude Code
  decides as it would without it). `deny` appears only when the policy has a block rule, and
  it has one only when the history contains enough bad calls to certify it. Here that is
  rare, so most of what the policy won't vouch for is `ask`.
- With `--grant`, the call the policy accepts becomes `allow`. With trimset down it is `ask`
  even with `--grant`.
- The MCP session: the server's name, three tools (`gate`, `outcome`, `explain`), a gate
  verdict with a decision id, and the policy in plain words, with what it would do at 97%.
- The decisions trimset recorded, with the actions taken.

## The stand-in model, and what it means

The decision model here is trimset's mock upstream, which answers from a hash of the call.
It does not understand commands: a Terraform apply and `pwd` get probabilities that have
nothing to do with how risky they are. What is real is everything around it: a policy
compiled and certified from labelled history, approved by a person, and applied by the hook
and the server with the failure behaviour above. Point the hook at a real model (any
`/v1/systemone` upstream, through the proxy) and the same policy gates what it says. The
policy is compiled from this run's history, so which of the nine calls the stand-in lets
through changes from run to run; the checks at the end of the script assert only what holds
whatever the thresholds are.

CI runs this script on every push, in the image job.
