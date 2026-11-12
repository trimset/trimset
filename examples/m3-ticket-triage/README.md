# M3 demo: ticket triage, from a policy to an app that obeys it

Support-ticket routing ([UC-2](../../docs/01-requirements.md#uc-2--ticket-triage)): a
model names the team for each ticket, trimset decides which answers the app can trust,
and an app using the Python SDK applies the policy locally, with no network call on the
request path.

```bash
examples/m3-ticket-triage/run.sh
```

It needs Docker, curl, openssl and python3, and takes a few minutes, most of it the first
build. It writes a `.env` with a random database password and secret if you don't have one.

## What it does

1. Starts trimset and creates a project with an admin key (the approver) and an ingest
   key (the support app).
2. Loads a month of tickets (6,000) from a sharp model, and the outcomes that came back:
   the team that took each ticket reports within 72 hours, and 5% never do.
3. `trimset calibrate`, then `trimset policy compile --error-budget 0.25`: at most 25%
   wrong on the tickets the policy handles by itself, with 95% confidence.
4. `trimset policy submit`, `approve` and `activate`, each with a reason on the record.
5. Starts the support app (`app.py`, the SDK's side). It routes six tickets, each gated by
   the live policy, logs each exchange with the action taken, and reports what each ticket
   turned out to need. Then it watches one borderline ticket.
6. Compiles a second, looser policy (a 40% error budget, on a model that is already
   about 32% wrong, so it automates every ticket) and reads its diff (`trimset policy diff`: thresholds,
   metrics, and who supplied the tuning labels). Activates it. The running app switches
   to it within about a poll, without a restart.
7. `trimset policy rollback`. The app goes back to version 1.
8. Prints what the app logged (`trimset decisions tail`) and the audit log.

## What you should see

- A table of six tickets with the action the policy took on each: clear ones handled by
  the policy, the unclear ones sent to a person.
- The app's watch line changing three times: `policy v1`, then `policy v2` a second or two
  after the activation, then `policy v1` again after the rollback.
- A diff for v2 with the thresholds before and after.
- An audit trail of nine policy events, in order: three for the first policy, then
  submit, approve, the first policy's retirement and the second's activation, and the two
  halves of the rollback, each with its reason.

The app polls every second here so the demo is quick. The SDK's default is 30 seconds
(plus or minus 20%), so a change reaches every app within 36 seconds, inside the 60 the
requirements promise ([sdk.md](../../docs/03-api/sdk.md) §4).

CI runs this script on every push, in the image job.
