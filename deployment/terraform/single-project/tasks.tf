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

resource "google_service_account_iam_member" "tasks_uses_task_invoker" {
  service_account_id = google_service_account.task_invoker.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${local.tasks_service_agent}"
}
