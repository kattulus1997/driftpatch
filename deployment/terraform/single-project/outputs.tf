output "region" {
  value = var.region
}

output "artifact_repository" {
  value = google_artifact_registry_repository.images.name
}

output "gross_usage_budget" {
  value = google_billing_budget.release.name
}

output "public_url" {
  value = google_cloud_run_v2_service.public.uri
}

output "worker_url" {
  value     = google_cloud_run_v2_service.worker.uri
  sensitive = true
}

output "admission_url" {
  value     = google_cloud_run_v2_service.admission.uri
  sensitive = true
}

output "result_url" {
  value     = google_cloud_run_v2_service.result.uri
  sensitive = true
}

output "firestore_database" {
  value = google_firestore_database.ledger.name
}

output "task_queue" {
  value = google_cloud_tasks_queue.worker.name
}

output "live_source_bucket" {
  value = google_storage_bucket.live_source.name
}

output "live_source_watch" {
  value = google_cloud_scheduler_job.live_source_watch.id
}
