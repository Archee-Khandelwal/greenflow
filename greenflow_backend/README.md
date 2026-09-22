# Carbon-aware workflow scheduler: Python backend

This is the same scheduling engine as the web app, as a Python module and an optional web service.
Given a workflow description (JSON), it chooses a **model size, region and start time for every step**
and returns a **receipt** for each step: energy, carbon (operational / transfer / hardware), cost,
expected accuracy, and the reasons for the decision.

> Everything is **simulated**. Carbon curves are synthetic unless you supply real ones, and energy,
> price and accuracy figures are assumptions unless you override them. Carbon uses average (not
> marginal) grid intensity. Do not present the numbers as measurements.

## Where the Small / Medium / Large numbers come from

The three model tiers are not arbitrary. Each figure below is checked against a real, cited source
and labeled by how well-grounded it is. `overrides.models` (see below) lets you replace any of
these with your own measured numbers.

| Figure | Small | Medium | Large | Confidence |
|---|---|---|---|---|
| Energy, kWh / 1,000 tokens | 0.0002 | 0.0008 | 0.003 | **Grounded** — see below |
| Price, $ / 1,000 tokens | 0.0004 | 0.002 | 0.008 | **Grounded** — see below |
| Embodied carbon, g CO2e / compute-hour | 20 | 60 | 250 | **Weakly grounded** — see below |

**Energy.** Epoch AI's 2025 measurements put LLM inference energy at **0.0001–0.002 Wh per output
token** across model sizes — which, per 1,000 tokens, is numerically the same range in kWh
(0.0001–0.002 kWh/1k tok). A September 2025 bottom-up study (H100-node power draw ÷ measured
throughput, PUE-adjusted) independently found a median of **0.34 Wh/query (IQR 0.18–0.67)** for
frontier-scale (>200B parameter) models, and OpenAI and Google's own disclosed averages are
**0.34 Wh** and **0.24 Wh** per typical query respectively. Our Small tier (0.0002) sits inside the
Epoch AI range near the efficient end; Medium (0.0008) lands close to the OpenAI/Google disclosed
per-query averages scaled to token count; Large (0.003) sits just above Epoch AI's upper bound,
consistent with the higher end of the frontier-model IQR from the bottom-up study. We did not
measure this ourselves — we picked values that fall inside a real, cited empirical range, and the
Small:Medium:Large ratio (1:4:15) is our own simplifying assumption about how tiers scale, not a
measured relationship.

**Price.** Public API pricing as of late 2025 / 2026 clusters into bands: budget models around
**$0.03–0.3 per million tokens**, mid-tier around **$0.5–3 per million**, and frontier/premium
models around **$5–30 per million** (blended input/output). Our figures — $0.4, $2, $8 per million
tokens for Small/Medium/Large — sit inside each published band.

**Embodied carbon (hardware manufacturing, amortized per compute-hour).** This is the weakest of
the three. The reasoning: full-system embodied carbon (chip + board + chassis + cooling +
networking, not just the die) amortized over a ~3-year hardware refresh, scaled roughly by how much
hardware each tier occupies (a fraction of one accelerator for Small, multiple for Large). We could
not find a tight, current primary citation for this figure in the time available. Say this directly
if asked: it is an order-of-magnitude illustrative assumption, not a benchmarked number, and it is
the one figure in this table we'd flag as needing more work before trusting it for a real decision.

Sources: Epoch AI (2025), inference energy per token; "Energy Use of AI Inference: Efficiency
Pathways and Test-Time Compute" (arXiv, Sept 2025), bottom-up per-query energy methodology;
OpenAI and Google public environmental disclosures (average/median query energy); public LLM API
pricing trackers, 2025–2026.

## What is in this folder

| File | What it is |
|---|---|
| `scheduler_core.py` | The engine. Plain Python, **no dependencies**. Also a command-line tool. |
| `scheduler_api.py` | A small web service around the engine (needs `fastapi` and `uvicorn`). |
| `client_example.py` | Shows how an agent framework would ask for a plan before running its steps. |
| `test_scheduler.py` | Checks with answers you can verify on paper. |
| `example_spec.json` | An example request (the document compliance workflow). |
| `requirements.txt` | `fastapi`, `uvicorn` (only needed for the web service). |

## Quick start (no installs needed)

```
python scheduler_core.py                      # schedule the built-in document workflow
python scheduler_core.py --workflow research  # or: docs, research, support
python scheduler_core.py example_spec.json    # schedule a request file
python scheduler_core.py --json               # print the full JSON response
python test_scheduler.py                      # run the checks (11 should pass)
python client_example.py --local              # see the agent-side integration
```

## Web service

```
pip install -r requirements.txt
python scheduler_api.py
```

Then open http://127.0.0.1:8000/docs for an interactive page, or call it directly:

| Method and path | What it does |
|---|---|
| `GET /health` | `{"status": "ok"}` |
| `GET /workflows` | Lists the built-in workflows |
| `GET /example?workflow=docs` | An example request (`docs`, `research` or `support`) |
| `POST /schedule` | Send a request, get a plan and receipts. Returns HTTP 422 with `errors` if the request is invalid |

With the service running, `python client_example.py` calls it.

## Request format

```json
{
  "workflow": {
    "name": "My workflow",
    "deadlineHours": 18,
    "steps": [
      {"id": "S1", "name": "Ingest", "type": "parse", "deps": [],
       "baseHours": 0.5, "kTokens": 500, "minAccuracy": 0.94,
       "deadlineHours": 2, "canWait": false, "dataGB": 20, "indiaOnly": false}
    ]
  },
  "submitAtIST": "20:30",
  "forecastError": 0.0,
  "cascades": {"enabled": false, "catchRate": 0.8, "hardPenalty": 0.2, "verifierEnergyShare": 0.1},
  "carbonProfiles": {"paris": [40, 38, 36, "... 24 hourly values, local time"]},
  "carbonSources": {"paris": "grid operator, mean of Sept 2026"},
  "overrides": {"models": {"large": {"kwh": 0.0035}}}
}
```

- `type` is one of `parse`, `classify`, `extract`, `reason`, `summarize`, `search`, `plan`.
- `baseHours` is the time for the Medium model at home (Small takes half, Large twice).
- `kTokens` is thousands of tokens for the step. `dataGB` is what moves if the step runs in another region.
- `canWait: false` means the step must start as soon as its inputs are ready.
- `indiaOnly: true` pins the step to Mumbai (data residency).
- `carbonProfiles` replaces a region's synthetic curve with 24 real hourly values in that region's local time.
- `overrides` changes any internal assumption (models, accuracy table, regions...).
- `valueOfSpeed` (grams CO2e you would pay to finish one hour sooner) and `valueOfCost` (grams per dollar) make speed and cost part of the objective. Both default to 0, which gives the carbon-only plan.
- `gridSpike` is `{"region": "paris", "startHours": 6, "durationHours": 10, "multiplier": 6}`. The plan is made without knowing about the spike, then the steps that have not started are re-planned when it hits. The response has a `spike` block with the original and re-planned carbon.

## Response format

`summary` (total carbon, saving against always-largest, finish time, late steps, accuracy, energy, cost)
and `receipts`, one per step, for example:

```json
{"step": "S5", "model_chain": ["Large"], "region": "Mumbai",
 "start_ist": "10:59 +1d", "carbon_g": {"operational": 14717.5, "transfer": 0, "hardware": 750, "total": 15467.5},
 "expected_accuracy": 0.92, "deadline_met": true,
 "reasons": ["Model: Small (0.62) and Medium (0.78) fall below the 0.90 accuracy floor. ...", "..."]}
```

## Known limits

- The scheduler is greedy (one step at a time), so it is not guaranteed to be globally optimal.
- No real LLM calls are made, and no real energy is measured.
- The service plans once per request. A grid spike is handled by re-planning inside that request (`gridSpike`), not by watching a live grid.
- The CORS setting in `scheduler_api.py` is fully open for demos. Restrict it before any real deployment.

The Python engine and the web app give the same numbers on the default document workflow (17,123.752 g), with cascades (8,305.809 g) and with the Paris grid spike (20,684.355 g original, 18,864.666 g re-planned). The tests check these.

## How it fits together

```
agent framework --POST /schedule--> scheduler_api.py --> scheduler_core.py
      ^                                                    |  1. options per step (model x region), filtered by accuracy floor and residency
      |                                                    |  2. slack from deadlines (backward pass)
      |                                                    |  3. greedy pick of the lowest-carbon option and start time
      |                                                    |  4. optional spike re-plan, cascades, speed and cost weights
      +------------- plan + receipts (JSON) <--------------+  5. lower bound and gap to it
inputs: workflow spec, carbon curves (synthetic or real via carbonProfiles), measured energy via overrides
```

`summary.lower_bound_g` is a number no schedule can beat (each step chosen on its own, ignoring step order). `gap_to_lower_bound_percent` says how far the plan can be from the true optimum. It is `null` when speed or cost carry a weight, because the bound covers carbon only.
