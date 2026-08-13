variable "project_id" {
  description = "Globally unique ID for the dedicated project created by this module."
  type        = string

  validation {
    condition     = can(regex("^driftpatch-[a-z0-9-]{6,20}$", var.project_id))
    error_message = "project_id must identify a new dedicated driftpatch-* project."
  }
}

variable "billing_account_id" {
  description = "Billing account attached to the dedicated project; no resources are created before apply."
  type        = string
  sensitive   = true
}

variable "region" {
  description = "Co-located Cloud Run and Firestore region nearest Spain."
  type        = string
  default     = "europe-west1"

  validation {
    condition     = var.region == "europe-west1"
    error_message = "The audited release must remain co-located in the nearest supported European Cloud Tasks region."
  }
}

variable "image" {
  description = "Immutable release image shared by the role-selected services."
  type        = string

  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.image))
    error_message = "image must use an immutable sha256 digest."
  }
}

variable "public_service_name" {
  type    = string
  default = "driftpatch"
}

variable "worker_service_name" {
  type    = string
  default = "driftpatch-worker"
}

variable "admission_service_name" {
  type    = string
  default = "driftpatch-admission"
}

variable "result_service_name" {
  type    = string
  default = "driftpatch-result"
}

variable "database_id" {
  type    = string
  default = "driftpatch"
}
