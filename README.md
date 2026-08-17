# DriftPatch

**Proof-carrying repair for public-data pipelines.**

Upload a previously working source, the current source, its pipeline
configuration and data contract. DriftPatch either activates a typed,
deterministically verified repair, confirms that nothing changed, or escalates
without mutating configuration.

**Try it:** [driftpatch.guillermozubikarai.dev](https://driftpatch.guillermozubikarai.dev)

![DriftPatch social preview](frontend/public/og-driftpatch.png)

Public feeds break consumers in small but consequential ways: CSV becomes JSON,
a delimiter changes, records move under a nested path, a source field is renamed
or a value vocabulary changes. DriftPatch handles those changes as constrained
configuration repair, not open-ended code generation.

```text
baseline + current source + pipeline + contract
    -> strict admission and baseline proof
    -> deterministic catalogue of authorized repair steps
    -> Gemini Embedding supplies abstaining field-lineage hints
    -> ADK planner selects opaque candidate identifiers
    -> bounded verifier-guided proposal loop
    -> independent full-data replay by the result controller
    -> atomic configuration, rollback snapshot and receipt
```

The model proposes; code authorizes. Structured output cannot make a failed
contract pass.

![DriftPatch production architecture](architecture.svg)

## Supported contract

Sources may be CSV or JSON in any baseline/current combination. The pipeline and
contract are strict JSON documents. A program may compose up to six observed,
typed changes:

- retarget an existing output to a renamed source field;
- switch CSV/JSON source format, CSV delimiter or JSON record path;
- select a lossless string, integer or number cast;
- update an allowlisted date parser or disjoint boolean vocabulary;
- reconstruct an output by joining fields or split one field into existing
  outputs.

Every accepted run ends as `repaired`, `unchanged` or `escalated`. Malformed,
ambiguous, unsupported or out-of-limit inputs are rejected before inference.
The published envelope is UTF-8 text, a 5 MiB request body, at most 20,000
records per source, 256 fields, 100,000 characters per cell and JSON depth 32.
CSV delimiters are comma, semicolon, pipe or tab. Admission is capped at 48
custom runs per UTC day, of which at most 24 may require inference.

This is a bounded repair language, not a claim that arbitrary formats or
transformations can be repaired safely.

## Authority and data boundary

- Admission first proves that the submitted baseline satisfies the submitted
  pipeline and contract. Without a working reference, no repair is admitted.
- Raw source bundles live briefly in a private Cloud Storage bucket. Tasks,
  Firestore, traces and model messages carry references, hashes, profiles and
  receipts—not source rows.
- Deterministic inspection creates the only candidate catalogue. Gemini 3.5
  Flash sees bounded structural evidence and opaque identifiers; it cannot
  create a new operation, execute code or access Cloud tools.
- `gemini-embedding-001` compares structural field labels only when multiple
  rename candidates exist. It emits one advisory hint only above calibrated
  similarity and margin thresholds; otherwise it abstains. It never sees rows,
  creates candidates or authorizes a repair.
- Model Armor screens planner input and output in `europe-west1`. A match,
  partial scan or unavailable screen fails closed.
- The result controller reloads the exact object generation, verifies its
  digest, rebuilds the catalogue and proves the unique shortest repair over the
  complete source. Equivalent model selections are canonicalized; redundant or
  ambiguous programs are rejected.
- Only the result controller may commit. Firestore atomically records the active
  configuration, previous version, rollback snapshot and terminal receipt.
- Terminal bundles are deleted by exact generation. A lifecycle rule removes
  abandoned objects after one day, with soft delete disabled.
- Cloud Trace retains operational spans with prompt and response content
  disabled.

Failed proposals receive up to three field-scoped counterexamples and the prior
candidate set, then may be retried for at most three rounds. Model and
deterministic search may terminate an escalation immediately only when both
agree that no unique repair exists. A bounded deterministic search over the same
authorized catalogue remains the terminating fallback. No fallback weakens a
contract, expands a limit or runs generated code.

## Production architecture

The public domain passes through Cloudflare to a dedicated Google Cloud project
in `europe-west1`. Four Cloud Run services use separate identities:

- the public interface can invoke admission and has no queue, ledger, storage or
  model authority;
- admission validates, reserves quota, stores the ephemeral bundle and creates
  a named Cloud Task;
- the internal worker has read-only bundle access, Model Armor use and Vertex AI
  prediction, but no ledger access;
- the result controller independently verifies and is the sole Firestore writer.

Cloud Tasks dispatches one worker request at a time. Cloud Scheduler watches the
curated private source every five minutes and invokes stale-run reconciliation
every ten minutes through distinct OIDC identities. Every service scales to zero
and is capped at one instance. Terraform preserves a €10 gross-usage alert
budget; the alert tracks usage before credits and is not represented as a hard
spending cap.

Cloud Tasks is unavailable in Madrid, so Belgium is the nearest shared region
for Cloud Run, Tasks, Firestore, Storage and Model Armor.

## Reproducible evidence

The custom corpus covers all four CSV/JSON transitions, unchanged input, every
atomic repair family, one-to-six-step programs and safe rejection or escalation
for ambiguity, duplicate keys and out-of-language changes. The untouched
nine-case holdout scores 9/9 on the deterministic decision metric and 5.0/5.0
on the trace-grounded response rubric:

- [`custom-holdout.json`](artifacts/traces/custom-holdout.json)
- [`decision results`](artifacts/grade_results/custom-holdout-rubric/results_20260814_010345.json)

A fresh Vertex AI run after the verifier-loop changes preserves those scores
while reducing planner calls from 19 to 13 (31.6%) on the same nine cases:

- [`credit baseline`](artifacts/grade_results/credit-baseline-20260817-enterprise/results_20260817_185602.json)
- [`optimized run`](artifacts/grade_results/credit-final-20260817/results_20260817_191452.json)

The frozen eight-case field-lineage holdout measures the additional embedding
model independently. Raw top-1 accuracy is 6/8; the calibrated 0.01 margin gate
emits six hints, all correct (100% precision, 75% coverage), and abstains on the
two errors:

- [`lineage holdout`](tests/eval/datasets/lineage-holdout.json)
- [`calibrated result`](artifacts/lineage/holdout-calibrated-20260817.json)

The corpus and expected terminal hashes are frozen in
[`benchmark/custom/manifest.json`](benchmark/custom/manifest.json). Property and
security tests cover row/key-order invariance, duplicate identifiers, ambiguous
aliases, dates and booleans, prompt-injection text, malformed UTF-8/JSON/CSV,
lease expiry, retries, quota races and bundle integrity.

## Run and verify

Requirements: Python 3.11–3.13, [uv](https://docs.astral.sh/uv/), Node.js 20+
and a Gemini API key for local inference.

```bash
cp .env.example .env
# Add GEMINI_API_KEY to the ignored .env file.
uv sync --extra lint
cd frontend && npm ci && npm run build && cd ..
uv run uvicorn app.fast_api_app:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000` and upload four files, or load the replaceable
example. Credentials stay in the ignored `.env` file.

```bash
uv run pytest tests/unit tests/integration tests/property tests/security -q
uv run ruff check app scripts tests
cd frontend && npm test -- --run && npm run build
terraform -chdir=deployment/terraform/single-project fmt -check
terraform -chdir=deployment/terraform/single-project validate
```

## Deploy

The release module owns the project, immutable Artifact Registry repository,
Cloud Run services, Firestore database, Cloud Tasks queue, schedulers, private
Storage buckets, Model Armor template, least-privilege IAM and budget alert.

```bash
export TF_VAR_project_id=driftpatch-<release-id>
export TF_VAR_billing_account_id=<billing-account-id>
export TF_VAR_image=example.invalid/driftpatch@sha256:0000000000000000000000000000000000000000000000000000000000000000

terraform -chdir=deployment/terraform/single-project init
terraform -chdir=deployment/terraform/single-project apply \
  -target=google_project.release \
  -target=google_project_service.required \
  -target=google_artifact_registry_repository.images
```

Build once, resolve the registry digest and apply the reviewed plan with that
immutable reference:

```bash
COMMIT=$(git rev-parse HEAD)
REPOSITORY=europe-west1-docker.pkg.dev/${TF_VAR_project_id}/driftpatch/app
gcloud builds submit --project "${TF_VAR_project_id}" --region europe-west1 \
  --config cloudbuild.yaml \
  --substitutions "_IMAGE=${REPOSITORY}:${COMMIT},_AGENT_VERSION=${COMMIT}" .
gcloud artifacts docker images list "${REPOSITORY}" \
  --project "${TF_VAR_project_id}" --include-tags \
  --filter="tags:${COMMIT}" --format='value(version)'

export TF_VAR_image=${REPOSITORY}@sha256:<reported-digest>
terraform -chdir=deployment/terraform/single-project plan \
  -out=/tmp/driftpatch-release.tfplan
terraform -chdir=deployment/terraform/single-project apply \
  /tmp/driftpatch-release.tfplan
```

Do not commit billing identifiers, Terraform state or plans. A release is
complete only after the public upload path, private IAM denials, queue and
scheduler contracts, Firestore receipt, object deletion, Model Armor verdict,
Cloud Trace span and immutable image are independently observed.

## Provenance

Work on DriftPatch began on 12 August 2026 for the All Things Agentic contest.
Google's Agents CLI generated the initial project layout; the workflow, bounded
repair domain, interface, evaluation and infrastructure were created during the
contest period. Development assistance is disclosed as permitted by the rules.

## License

Apache License 2.0. See [`LICENSE`](LICENSE).
