locals {
  required_apis = toset([
    "aiplatform.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudscheduler.googleapis.com",
    "cloudtrace.googleapis.com",
    "firestore.googleapis.com",
    "iam.googleapis.com",
    "cloudtasks.googleapis.com",
    "run.googleapis.com",
    "storage.googleapis.com",
    "telemetry.googleapis.com",
  ])
  tasks_service_agent = "service-${google_project.release.number}@gcp-sa-cloudtasks.iam.gserviceaccount.com"
  worker_queue_name   = "driftpatch-worker"
}

resource "google_project" "release" {
  name                = "DriftPatch"
  project_id          = var.project_id
  billing_account     = var.billing_account_id
  auto_create_network = false
  deletion_policy     = "PREVENT"

  labels = {
    workload = "driftpatch"
    boundary = "dedicated"
  }
}

resource "google_project_service" "required" {
  for_each = local.required_apis

  project            = google_project.release.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_firestore_database" "ledger" {
  project                     = google_project.release.project_id
  name                        = var.database_id
  location_id                 = var.region
  type                        = "FIRESTORE_NATIVE"
  deletion_policy             = "ABANDON"
  delete_protection_state     = "DELETE_PROTECTION_ENABLED"
  app_engine_integration_mode = "DISABLED"

  depends_on = [google_project_service.required]
}

resource "google_service_account" "public" {
  project      = google_project.release.project_id
  account_id   = "driftpatch-public"
  display_name = "DriftPatch public judge surface"
}

resource "google_service_account" "admission" {
  project      = google_project.release.project_id
  account_id   = "driftpatch-admission"
  display_name = "DriftPatch private admission controller"
}

resource "google_service_account" "worker" {
  project      = google_project.release.project_id
  account_id   = "driftpatch-worker"
  display_name = "DriftPatch private agent worker"
}

resource "google_service_account" "result" {
  project      = google_project.release.project_id
  account_id   = "driftpatch-result"
  display_name = "DriftPatch deterministic result controller"
}

resource "google_service_account" "task_invoker" {
  project      = google_project.release.project_id
  account_id   = "driftpatch-push"
  display_name = "DriftPatch authenticated task invoker"
}

resource "google_service_account" "scheduler_invoker" {
  project      = google_project.release.project_id
  account_id   = "driftpatch-scheduler"
  display_name = "DriftPatch scheduled source watcher"
}

resource "google_storage_bucket" "live_source" {
  project                     = google_project.release.project_id
  name                        = "${google_project.release.project_id}-live-source"
  location                    = var.region
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      num_newer_versions = 10
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.required]
}

resource "google_storage_bucket_object" "live_source" {
  name         = "column-rename.csv"
  bucket       = google_storage_bucket.live_source.name
  content      = file("${path.module}/../../../benchmark/fixtures/column-rename-before.csv")
  content_type = "text/csv"
}

resource "google_storage_bucket_iam_member" "admission_source_reader" {
  bucket = google_storage_bucket.live_source.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.admission.email}"
}

resource "google_cloud_run_v2_service" "public" {
  project             = google_project.release.project_id
  name                = var.public_service_name
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = true

  template {
    service_account                  = google_service_account.public.email
    timeout                          = "30s"
    max_instance_request_concurrency = 40

    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }

    containers {
      image = var.image

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle = true
      }

      env {
        name  = "SERVICE_ROLE"
        value = "public"
      }
      env {
        name  = "ADMISSION_URL"
        value = google_cloud_run_v2_service.admission.uri
      }
      startup_probe {
        failure_threshold     = 10
        initial_delay_seconds = 0
        period_seconds        = 3
        timeout_seconds       = 2

        http_get {
          path = "/health"
          port = 8080
        }
      }
    }
  }

  depends_on = [google_project_service.required]
}

resource "google_cloud_run_v2_service" "admission" {
  project             = google_project.release.project_id
  name                = var.admission_service_name
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = true

  template {
    service_account                  = google_service_account.admission.email
    timeout                          = "30s"
    max_instance_request_concurrency = 40

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      image = var.image

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle = true
      }

      env {
        name  = "SERVICE_ROLE"
        value = "admission"
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = google_project.release.project_id
      }
      env {
        name  = "CLOUD_TASKS_LOCATION"
        value = var.region
      }
      env {
        name  = "CLOUD_TASKS_QUEUE"
        value = google_cloud_tasks_queue.worker.name
      }
      env {
        name  = "WORKER_URL"
        value = google_cloud_run_v2_service.worker.uri
      }
      env {
        name  = "TASK_INVOKER_SERVICE_ACCOUNT"
        value = google_service_account.task_invoker.email
      }
      env {
        name  = "FIRESTORE_ENABLED"
        value = "true"
      }
      env {
        name  = "FIRESTORE_DATABASE"
        value = google_firestore_database.ledger.name
      }
      env {
        name  = "LIVE_SOURCE_BUCKET"
        value = google_storage_bucket.live_source.name
      }
      env {
        name  = "LIVE_SOURCE_OBJECT"
        value = google_storage_bucket_object.live_source.name
      }

      startup_probe {
        failure_threshold     = 10
        initial_delay_seconds = 0
        period_seconds        = 3
        timeout_seconds       = 2

        http_get {
          path = "/health"
          port = 8080
        }
      }
    }
  }

  depends_on = [google_project_service.required]
}

resource "google_cloud_run_v2_service" "worker" {
  project             = google_project.release.project_id
  name                = var.worker_service_name
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  deletion_protection = true

  template {
    service_account                  = google_service_account.worker.email
    timeout                          = "600s"
    max_instance_request_concurrency = 1

    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }

    containers {
      image = var.image

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
        cpu_idle = true
      }

      env {
        name  = "SERVICE_ROLE"
        value = "worker"
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = google_project.release.project_id
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = "global"
      }
      env {
        name  = "GOOGLE_GENAI_USE_ENTERPRISE"
        value = "TRUE"
      }
      env {
        name  = "CLOUD_TASKS_QUEUE"
        value = google_cloud_tasks_queue.worker.name
      }
      env {
        name  = "RESULT_URL"
        value = google_cloud_run_v2_service.result.uri
      }
      env {
        name  = "CLOUD_TELEMETRY_ENABLED"
        value = "true"
      }
      env {
        name  = "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"
        value = "false"
      }
      env {
        name  = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
        value = "NO_CONTENT"
      }

      startup_probe {
        failure_threshold     = 20
        initial_delay_seconds = 0
        period_seconds        = 3
        timeout_seconds       = 2

        http_get {
          path = "/health"
          port = 8080
        }
      }
    }
  }

  depends_on = [google_project_service.required]
}

resource "google_cloud_run_v2_service" "result" {
  project             = google_project.release.project_id
  name                = var.result_service_name
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = true

  template {
    service_account                  = google_service_account.result.email
    timeout                          = "30s"
    max_instance_request_concurrency = 10

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      image = var.image

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle = true
      }

      env {
        name  = "SERVICE_ROLE"
        value = "result"
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = google_project.release.project_id
      }
      env {
        name  = "FIRESTORE_ENABLED"
        value = "true"
      }
      env {
        name  = "FIRESTORE_DATABASE"
        value = google_firestore_database.ledger.name
      }

      startup_probe {
        failure_threshold     = 10
        initial_delay_seconds = 0
        period_seconds        = 3
        timeout_seconds       = 2

        http_get {
          path = "/health"
          port = 8080
        }
      }
    }
  }

  depends_on = [google_project_service.required]
}

resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  project  = google_project.release.project_id
  location = var.region
  name     = google_cloud_run_v2_service.public.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_v2_service_iam_member" "task_worker_invoker" {
  project  = google_project.release.project_id
  location = var.region
  name     = google_cloud_run_v2_service.worker.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.task_invoker.email}"
}

resource "google_cloud_run_v2_service_iam_member" "public_admission_invoker" {
  project  = google_project.release.project_id
  location = var.region
  name     = google_cloud_run_v2_service.admission.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.public.email}"
}

resource "google_cloud_run_v2_service_iam_member" "scheduler_admission_invoker" {
  project  = google_project.release.project_id
  location = var.region
  name     = google_cloud_run_v2_service.admission.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_invoker.email}"
}

resource "google_cloud_scheduler_job" "live_source_watch" {
  project          = google_project.release.project_id
  region           = var.region
  name             = "driftpatch-live-source-watch"
  description      = "Checks the bounded live source and admits a repair only for a recognized content digest."
  schedule         = "*/5 * * * *"
  time_zone        = "Etc/UTC"
  attempt_deadline = "30s"

  retry_config {
    retry_count          = 1
    min_backoff_duration = "10s"
    max_backoff_duration = "30s"
    max_doublings        = 0
  }

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.admission.uri}/internal/watch"
    body        = base64encode("{}")
    headers = {
      "Content-Type" = "application/json"
    }

    oidc_token {
      service_account_email = google_service_account.scheduler_invoker.email
      audience              = google_cloud_run_v2_service.admission.uri
    }
  }

  depends_on = [
    google_cloud_run_v2_service_iam_member.scheduler_admission_invoker,
    google_project_service.required,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "worker_result_invoker" {
  project  = google_project.release.project_id
  location = var.region
  name     = google_cloud_run_v2_service.result.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_project_iam_custom_role" "worker_vertex_predictor" {
  project     = google_project.release.project_id
  role_id     = "driftpatchVertexPredictor"
  title       = "DriftPatch Vertex predictor"
  description = "Invokes publisher models without managing Vertex AI resources."
  permissions = [
    "aiplatform.endpoints.predict",
    "resourcemanager.projects.get",
  ]
}

resource "google_project_iam_member" "worker_vertex_predictor" {
  project = google_project.release.project_id
  role    = google_project_iam_custom_role.worker_vertex_predictor.id
  member  = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_project_iam_member" "worker_telemetry_writer" {
  project = google_project.release.project_id
  role    = "roles/telemetry.tracesWriter"
  member  = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_project_iam_member" "worker_service_usage_consumer" {
  project = google_project.release.project_id
  role    = "roles/serviceusage.serviceUsageConsumer"
  member  = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_project_iam_custom_role" "admission_ledger_controller" {
  project     = google_project.release.project_id
  role_id     = "driftpatchAdmissionController"
  title       = "DriftPatch admission controller"
  description = "Creates and releases bounded admissions and reads terminal status."
  permissions = [
    "datastore.databases.get",
    "datastore.entities.create",
    "datastore.entities.delete",
    "datastore.entities.get",
    "datastore.entities.update",
  ]
}

resource "google_project_iam_member" "admission_ledger_controller" {
  project = google_project.release.project_id
  role    = google_project_iam_custom_role.admission_ledger_controller.id
  member  = "serviceAccount:${google_service_account.admission.email}"

  condition {
    title       = "driftpatch-ledger-only"
    description = "Restricts the admission controller to the dedicated ledger database."
    expression  = "resource.name == \"projects/${google_project.release.project_id}/databases/${google_firestore_database.ledger.name}\""
  }
}

resource "google_project_iam_custom_role" "result_ledger_committer" {
  project     = google_project.release.project_id
  role_id     = "driftpatchResultCommitter"
  title       = "DriftPatch result committer"
  description = "Reads an admitted document and replaces it with deterministic terminal evidence."
  permissions = [
    "datastore.databases.get",
    "datastore.entities.get",
    "datastore.entities.update",
  ]
}

resource "google_project_iam_member" "result_ledger_committer" {
  project = google_project.release.project_id
  role    = google_project_iam_custom_role.result_ledger_committer.id
  member  = "serviceAccount:${google_service_account.result.email}"

  condition {
    title       = "driftpatch-ledger-only"
    description = "Restricts the result controller to the dedicated ledger database."
    expression  = "resource.name == \"projects/${google_project.release.project_id}/databases/${google_firestore_database.ledger.name}\""
  }
}
