# M2 demo: refund fraud, from decisions to a guarantee

Refund fraud ([UC-1](../../docs/01-requirements.md#uc-1--refund-fraud)), from logged
decisions to a policy with a written guarantee, all through the `trimset` CLI.

```bash
examples/m2-refund-fraud/run.sh
```

It needs Docker, curl and openssl, and takes a few minutes, most of it the first build.
It writes a `.env` with a random database password and secret if you don't have one.

## What it does

1. Starts trimset (Postgres, proxy, server, worker), plus the image that holds the data
   generator.
2. Creates a project with an admin key and an ingest key.
3. Loads seven months of refunds (12,600) from a model that is badly calibrated: it says
   0.3 where the truth is closer to 0.05. Chargebacks arrive 30 to 90 days late, the
   risk team reviews the refunds it was sent, and 1% are audited. A refund with no
   chargeback after 120 days is labelled "not fraud" by a default rule.
4. Sets the question's label horizon to 120 days, so only refunds old enough to have had
   their chargeback count for fitting, and runs the default rule.
5. `trimset coverage`: labels by action path. Held refunds never get one, and trimset
   says so.
6. `trimset calibrate`: fits a calibration map on 40% of the matured refunds, chosen by
   cross-validation, and reports log loss, Brier and ECE, raw against calibrated, on 20%
   it never saw.
7. `trimset simulate`: what "accept below 0.30, block from 0.85" would have done.
8. `trimset policy compile`: at most 2% of paid refunds fraudulent with 95% confidence,
   and at most 15% sent to the risk team. Thresholds are tuned on the remaining 40% by
   Learn-then-Test with exact binomial tests.

## What you should see

- Coverage: labels on ACCEPT and ESCALATE, and a warning that BLOCK has almost none.
- Calibration: the calibrated ECE far below the raw one.
- A draft policy: an ACCEPT rule, a BLOCK rule that blocks as little as the 15% cap
  allows, and the guarantee in words, with the number of refunds it rests on and the
  window.

The policy stays a draft: approving and activating it, and the SDK's `gate()` that
applies it, come in M3.

CI runs this script on every push, in the image job.
