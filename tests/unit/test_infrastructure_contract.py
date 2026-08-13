from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TERRAFORM = ROOT / "deployment" / "terraform" / "single-project"


def _source() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(TERRAFORM.glob("*.tf"))
    )


def test_worker_has_no_public_invoker_binding() -> None:
    source = _source()
    public_binding = source.split(
        'resource "google_cloud_run_v2_service_iam_member" "public_invoker"', 1
    )[1].split("resource ", 1)[0]
    worker_binding = source.split(
        'resource "google_cloud_run_v2_service_iam_member" "task_worker_invoker"',
        1,
    )[1].split("resource ", 1)[0]

    assert "google_cloud_run_v2_service.public.name" in public_binding
    assert 'member   = "allUsers"' in public_binding
    assert "google_cloud_run_v2_service.worker.name" in worker_binding
    assert "google_service_account.task_invoker.email" in worker_binding
    assert "allUsers" not in worker_binding
    assert source.count('member   = "allUsers"') == 1
    worker_service = source.split(
        'resource "google_cloud_run_v2_service" "worker"', 1
    )[1].split("resource ", 1)[0]
    assert 'ingress             = "INGRESS_TRAFFIC_INTERNAL_ONLY"' in worker_service
    assert "FIRESTORE_ENABLED" not in worker_service
    assert 'name  = "RESULT_URL"' in worker_service


def test_public_can_only_invoke_admission_and_worker_can_only_invoke_result() -> None:
    source = _source()
    public_service = source.split(
        'resource "google_cloud_run_v2_service" "public"', 1
    )[1].split("resource ", 1)[0]
    public_binding = source.split(
        'resource "google_cloud_run_v2_service_iam_member" "public_admission_invoker"',
        1,
    )[1].split("resource ", 1)[0]
    worker_binding = source.split(
        'resource "google_cloud_run_v2_service_iam_member" "worker_result_invoker"',
        1,
    )[1].split("resource ", 1)[0]

    assert 'name  = "ADMISSION_URL"' in public_service
    assert "FIRESTORE_ENABLED" not in public_service
    assert "CLOUD_TASKS_QUEUE" not in public_service
    assert "google_cloud_run_v2_service.admission.name" in public_binding
    assert "google_service_account.public.email" in public_binding
    assert "google_cloud_run_v2_service.result.name" in worker_binding
    assert "google_service_account.worker.email" in worker_binding


def test_cloud_tasks_is_named_rate_limited_and_uses_oidc() -> None:
    source = _source()

    assert 'resource "google_cloud_tasks_queue" "worker"' in source
    assert "max_dispatches_per_second = 1" in source
    assert "max_concurrent_dispatches = 1" in source
    assert 'role     = "roles/cloudtasks.enqueuer"' in source
    assert 'role               = "roles/iam.serviceAccountUser"' in source
    assert 'name  = "TASK_INVOKER_SERVICE_ACCOUNT"' in source
    enqueuer = source.split(
        'resource "google_cloud_tasks_queue_iam_member" "admission_enqueuer"', 1
    )[1].split("resource ", 1)[0]
    act_as = source.split(
        'resource "google_service_account_iam_member" "admission_uses_task_invoker"',
        1,
    )[1].split("resource ", 1)[0]
    assert "google_service_account.admission.email" in enqueuer
    assert "google_service_account.public.email" not in enqueuer
    assert "google_service_account.admission.email" in act_as
    assert "google_service_account.public.email" not in act_as
    assert 'resource "google_project_iam_member" "admission_service_usage_consumer"' in source


def test_live_source_watch_is_private_scheduled_and_digest_bounded() -> None:
    source = _source()

    assert '"cloudscheduler.googleapis.com"' in source
    assert '"storage.googleapis.com"' in source
    assert 'resource "google_cloud_scheduler_job" "live_source_watch"' in source
    assert 'schedule         = "*/5 * * * *"' in source
    assert 'time_zone        = "Etc/UTC"' in source
    assert 'http_method = "POST"' in source
    assert 'uri         = "${google_cloud_run_v2_service.admission.uri}/internal/watch"' in source
    assert "service_account_email = google_service_account.scheduler_invoker.email" in source
    assert "audience              = google_cloud_run_v2_service.admission.uri" in source
    assert 'public_access_prevention    = "enforced"' in source
    assert 'uniform_bucket_level_access = true' in source
    assert "num_newer_versions = 10" in source
    assert 'role   = "roles/storage.objectViewer"' in source
    assert "google_service_account.admission.email" in source
    assert 'name  = "LIVE_SOURCE_BUCKET"' in source
    assert 'name  = "LIVE_SOURCE_OBJECT"' in source

    scheduler_binding = source.split(
        'resource "google_cloud_run_v2_service_iam_member" "scheduler_admission_invoker"',
        1,
    )[1].split("resource ", 1)[0]
    assert "google_cloud_run_v2_service.admission.name" in scheduler_binding
    assert "google_service_account.scheduler_invoker.email" in scheduler_binding
    assert "allUsers" not in scheduler_binding


def test_terraform_lockfile_is_preserved_with_checksums() -> None:
    lockfile = TERRAFORM / ".terraform.lock.hcl"
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    ignored = subprocess.run(
        ["git", "check-ignore", str(lockfile.relative_to(ROOT))],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert lockfile.is_file()
    assert ignored.returncode == 1
    assert "**/.terraform/" in ignore
    assert "*.terraform*" not in ignore
    assert lockfile.read_text(encoding="utf-8").count('version     = "7.44.0"') == 1


def test_release_is_bounded_and_co_located_in_the_supported_european_region() -> None:
    source = _source()

    assert 'default     = "europe-west1"' in source
    assert "location_id                 = var.region" in source
    assert source.count("min_instance_count = 0") == 4
    assert source.count("max_instance_count = 1") == 4
    assert 'max_attempts       = 5' in source
    assert 'min_backoff        = "10s"' in source
    assert 'max_backoff        = "600s"' in source


def test_release_images_are_immutable_scanned_and_retained_by_policy() -> None:
    source = _source()

    assert 'resource "google_artifact_registry_repository" "images"' in source
    assert 'repository_id   = "driftpatch"' in source
    assert 'format          = "DOCKER"' in source
    assert 'deletion_policy = "PREVENT"' in source
    assert "immutable_tags = true" in source
    assert 'enablement_config = "INHERITED"' in source
    assert 'id     = "delete-untagged"' in source
    assert 'id     = "keep-recent"' in source


def test_gross_usage_budget_tracks_spend_before_credits() -> None:
    source = _source()

    assert 'resource "google_billing_budget" "release"' in source
    assert 'display_name    = "DriftPatch gross usage"' in source
    assert 'credit_types_treatment = "EXCLUDE_ALL_CREDITS"' in source
    assert 'currency_code = "EUR"' in source
    assert 'units         = "10"' in source
    for threshold in ("0.25", "0.5", "0.8", "1.0"):
        assert f"threshold_percent = {threshold}" in source


def test_public_identity_cannot_invoke_the_model() -> None:
    source = _source()
    vertex_role = source.split(
        'resource "google_project_iam_custom_role" "worker_vertex_predictor"', 1
    )[1].split("resource ", 1)[0]
    vertex_binding = source.split(
        'resource "google_project_iam_member" "worker_vertex_predictor"', 1
    )[1].split("resource ", 1)[0]

    assert '"aiplatform.endpoints.predict"' in vertex_role
    assert "aiplatform.endpoints.create" not in vertex_role
    assert "google_service_account.worker.email" in vertex_binding
    assert "google_service_account.public.email" not in vertex_binding


def test_only_the_bounded_worker_exports_cloud_traces() -> None:
    source = _source()
    worker_service = source.split(
        'resource "google_cloud_run_v2_service" "worker"', 1
    )[1].split("resource ", 1)[0]

    assert '"telemetry.googleapis.com"' in source
    assert '"cloudtrace.googleapis.com"' in source
    assert worker_service.count('name  = "CLOUD_TELEMETRY_ENABLED"') == 1
    assert 'value = "true"' in worker_service
    assert source.count('name  = "CLOUD_TELEMETRY_ENABLED"') == 1
    assert worker_service.count(
        'name  = "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"'
    ) == 1
    assert worker_service.count(
        'name  = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"'
    ) == 1
    assert 'value = "false"' in worker_service
    assert 'value = "NO_CONTENT"' in worker_service
    assert 'role    = "roles/telemetry.tracesWriter"' in source
    assert 'role    = "roles/serviceusage.serviceUsageConsumer"' in source
    telemetry_bindings = source.split(
        'resource "google_project_iam_member" "worker_telemetry_writer"', 1
    )[1].split(
        'resource "google_project_iam_custom_role" "admission_ledger_controller"',
        1,
    )[0]
    assert "google_service_account.worker.email" in telemetry_bindings
    assert "google_service_account.public.email" not in telemetry_bindings


def test_only_control_services_receive_ledger_permissions() -> None:
    source = _source()
    admission_role = source.split(
        'resource "google_project_iam_custom_role" "admission_ledger_controller"', 1
    )[1].split("resource ", 1)[0]
    admission_binding = source.split(
        'resource "google_project_iam_member" "admission_ledger_controller"', 1
    )[1].split("resource ", 1)[0]
    result_role = source.split(
        'resource "google_project_iam_custom_role" "result_ledger_committer"', 1
    )[1].split("resource ", 1)[0]
    result_binding = source.split(
        'resource "google_project_iam_member" "result_ledger_committer"', 1
    )[1].split("resource ", 1)[0]

    for permission in ("create", "delete", "get", "update"):
        assert f'"datastore.entities.{permission}"' in admission_role
    assert '"datastore.entities.list"' not in admission_role
    assert '"datastore.entities.get"' in result_role
    assert '"datastore.entities.update"' in result_role
    assert '"datastore.entities.create"' in result_role
    assert '"datastore.entities.delete"' not in result_role
    assert '"datastore.entities.list"' not in result_role
    assert "google_service_account.admission.email" in admission_binding
    assert "google_service_account.result.email" in result_binding
    assert 'resource "google_project" "release"' in source
    assert "project = google_project.release.project_id" in admission_binding
    assert "project = google_project.release.project_id" in result_binding
    assert 'auto_create_network = false' in source
    assert 'deletion_policy     = "PREVENT"' in source
    assert 'boundary = "dedicated"' in source
    assert 'condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$"' in source
    assert "created or explicitly imported into this module" in source
    database_condition = (
        'resource.name == \\"projects/${google_project.release.project_id}'
        '/databases/${google_firestore_database.ledger.name}\\"'
    )
    assert database_condition in admission_binding
    assert database_condition in result_binding
    assert "google_service_account.public.email" not in admission_binding + result_binding
    assert "google_service_account.worker.email" not in admission_binding + result_binding
    assert 'role    = "roles/datastore.user"' not in source


def test_release_container_builds_the_interface_and_runs_without_root() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM node:22-slim@sha256:" in dockerfile
    assert dockerfile.count("FROM python:3.12-alpine3.22@sha256:") == 1
    assert "RUN apk upgrade --no-cache" in dockerfile
    assert "FROM ghcr.io/astral-sh/uv:0.8.13@sha256:" in dockerfile
    assert "pip install" not in dockerfile
    assert "FROM alpine:3.22@sha256:" in dockerfile
    assert "apk add --no-cache ca-certificates libstdc++ python3" in dockerfile
    assert 'ENTRYPOINT ["python3"]' in dockerfile
    assert "RUN npm run build" in dockerfile
    assert "COPY benchmark/ ./benchmark/" in dockerfile
    assert "COPY --from=frontend /build/frontend/dist ./frontend/dist" in dockerfile
    assert "USER 65532:65532" in dockerfile
    assert "uv sync --frozen --no-dev --no-install-project" in dockerfile


def test_cloud_build_uses_a_dedicated_identity_and_commit_version() -> None:
    source = _source()
    build = (ROOT / "cloudbuild.yaml").read_text(encoding="utf-8")

    assert 'resource "google_service_account" "build"' in source
    assert 'role    = "roles/cloudbuild.builds.builder"' in source
    assert "driftpatch-build@$PROJECT_ID.iam.gserviceaccount.com" in build
    assert "AGENT_VERSION=${_AGENT_VERSION}" in build
    assert "logging: CLOUD_LOGGING_ONLY" in build
