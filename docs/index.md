# trimset documentation

trimset keeps the thresholds on a decision model's probabilities true. It logs every
decision, joins the outcome when it arrives, compiles a policy with a stated guarantee,
and lets your app apply the approved policy locally. It is open source (Apache-2.0) and
self-hosted: one `docker compose up`, Postgres underneath.

!!! note "Alpha"
    The docs follow the code. Pages for drift alerts, shadow models and the review queue
    arrive with those features.

## Start here

<div class="grid cards" markdown>

- **[Quickstart](quickstart.md)**

    From a first call to a policy deciding in your code, with a stand-in model. No
    account needed.

- **[Self-hosting](self-hosting.md)**

    What runs, TLS, every setting, backups and upgrades.

- **[SDK interface](03-api/sdk.md)**

    `gate()`, `log_exchange()`, `outcome()` and the algorithm `gate()` applies.

- **[API reference](api-reference.md)**

    Every endpoint, rendered from the OpenAPI contract that CI checks the server against.

</div>

## How it works

[How it fits together](02-architecture.md) shows the pieces and the flows between them.
The [decisions](adr/0001-server-language.md) behind it are written down, one per page.
The statistics (calibration, the compiler and its guarantee) are in
[ADR 0005](adr/0005-stats-library.md) and [ADR 0006](adr/0006-policy-compiler.md), with
sources in [research](research.md).
