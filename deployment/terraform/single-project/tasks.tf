resource "google_cloud_tasks_queue" "worker" {
  project  = google_project.release.project_id
  name     = local.worker_queue_name
  location = var.region

  rate_limits {
    max_dispatches_per_second = 1
    max_concurrent_dispatches = 1
  }

  retry_config {
    max_attempts       = 5
    max_retry_duration = "3600s"
    min_backoff        = "10s"
    max_backoff        = "600s"
    max_doublings      = 4
  }

  depends_on = [google_project_service.required]
}

resource "google_cloud_tasks_queue_iam_member" "admission_enqueuer" {
  project  = google_project.release.project_id
  location = var.region
  name     = google_cloud_tasks_queue.worker.name
  role     = "roles/cloudtasks.enqueuer"
  member   = "serviceAccount:${google_service_account.admission.email}"
}

resource "google_service_account_iam_member" "admission_uses_task_invoker" {
  service_account_id = google_service_account.task_invoker.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.admission.email}"
}

resource "google_cloud_scheduler_job" "custom_reconciler" {
  project          = google_project.release.project_id
  region           = var.region
  name             = "driftpatch-custom-reconciler"
  description      = "Terminalizes expired custom runs and removes their ephemeral bundles."
  schedule         = "*/10 * * * *"
  time_zone        = "Etc/UTC"
  attempt_deadline = "30s"

  retry_config {
    retry_count          = 1
    min_backoff_duration = "10s"
    max_backoff_duration = "30s"
    max_doublings        = 1
  }

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.result.uri}/internal/reconcile"
    body        = base64encode("{}")
    headers = {
      "Content-Type" = "application/json"
    }

    oidc_token {
      service_account_email = google_service_account.reconciler_invoker.email
      audience              = google_cloud_run_v2_service.result.uri
    }
  }

  depends_on = [
    google_cloud_run_v2_service_iam_member.reconciler_result_invoker,
    google_project_service.required,
  ]
}
