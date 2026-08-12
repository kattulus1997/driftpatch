# DriftPatch

**A bounded repair agent for public-data pipeline drift.**

Public datasets change without warning: a column is renamed, a delimiter moves,
or a JSON collection is nested one level deeper. DriftPatch turns that breakage
into a proof-carrying repair proposal. It inspects the change, asks Gemini 3.5
Flash to select one typed operation, applies it in memory, and lets deterministic
data contracts decide whether the result is safe to propose.

![DriftPatch social preview](frontend/public/og-driftpatch.png)

## The decision path

```text
incident event
    -> inspect before/after source profiles
    -> choose one allowlisted repair or escalate
    -> apply the proposal in memory
    -> run deterministic schema, type, row and uniqueness contracts
    -> record the evidence and terminal state
```

The model proposes; code authorizes. A fluent answer can never make a failing
contract pass.

## Enforced boundaries

- Exactly one typed operation from an explicit allowlist.
- No arbitrary code execution, shell access or automatic merge.
- Existing output fields may be retargeted, but new facts cannot be invented.
- Unsafe or ambiguous incidents terminate as `escalated`.
- An event identifier becomes the ledger key, so replay updates one evidence
  record rather than creating duplicates.
- A repair is successful only when every deterministic contract passes.

## Reproducible evidence

The repository contains ten small, hand-checkable incidents: eight safe repair
patterns and two cases that must escalate. The complete trace is in
[`artifacts/traces/full_benchmark.json`](artifacts/traces/full_benchmark.json),
and the final deterministic grade is in
[`artifacts/grade_results/final_10_deterministic/results_20260812_115831.json`](artifacts/grade_results/final_10_deterministic/results_20260812_115831.json).

That benchmark proves the current contracts against controlled fixtures. It is
not presented as real-world reliability evidence; held-out public sources and
production replay tests remain required before deployment.

## Run locally

Requirements: Python 3.11–3.13, [uv](https://docs.astral.sh/uv/), Node.js 20+
and either Google Cloud application credentials or a Gemini API key.

```bash
cp .env.example .env
uv sync --extra lint
cd frontend && npm ci && npm run build && cd ..
uv run uvicorn app.fast_api_app:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`, choose an incident, and run the complete live
decision path. Credentials stay in the ignored `.env` file.

## Verify

```bash
uv run ruff check app tests
uv run pytest tests/unit tests/integration -q
cd frontend && npm test -- --run && npm run build
```

## Architecture status

The local prototype uses the Agent Development Kit, FastAPI, an in-memory
session service and an in-memory evidence ledger. The production design targets
an event-driven worker on Cloud Run in Madrid, private Pub/Sub delivery,
transactional Firestore evidence, Cloud Logging and Cloud Trace. Those cloud
components are targets, not claims of current deployment; this section will be
updated only after the public path is verified end to end.

## Provenance

Work on DriftPatch began on 12 August 2026 for the All Things Agentic contest.
The initial project layout was generated with Google's Agents CLI; the agent
workflow, bounded repair domain, benchmark, interface and evidence were created
during the contest period. Development was AI-assisted.

## License

Apache License 2.0. See [`LICENSE`](LICENSE).
