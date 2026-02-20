terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

# ---------------------------------------------------------------------------
# Variables — fill these in via terraform.tfvars or -var flags
# ---------------------------------------------------------------------------

variable "project_id" {
  type        = string
  description = "GCP project ID"
}

variable "region" {
  type        = string
  default     = "us-east1"
  description = "GCP region for all resources"
}

variable "telemetry_secret" {
  type        = string
  sensitive   = true
  description = "HMAC shared secret for request signing"
}

variable "backup_bucket_name" {
  type        = string
  description = "Globally unique name for the Cloud Storage backup bucket"
}

# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

provider "google" {
  project = var.project_id
  region  = var.region
}

# ---------------------------------------------------------------------------
# Enable required APIs
# ---------------------------------------------------------------------------

resource "google_project_service" "run" {
  service            = "run.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "firestore" {
  service            = "firestore.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "storage" {
  service            = "storage.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "scheduler" {
  service            = "cloudscheduler.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "cloudbuild" {
  service            = "cloudbuild.googleapis.com"
  disable_on_destroy = false
}

# ---------------------------------------------------------------------------
# Firestore database
# ---------------------------------------------------------------------------

resource "google_firestore_database" "telemetry" {
  name        = "(default)"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"

  depends_on = [google_project_service.firestore]
}

# ---------------------------------------------------------------------------
# Cloud Storage bucket for JSONL backups
# ---------------------------------------------------------------------------

resource "google_storage_bucket" "backups" {
  name     = var.backup_bucket_name
  location = var.region

  uniform_bucket_level_access = true

  lifecycle_rule {
    condition {
      age = 365
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.storage]
}

# ---------------------------------------------------------------------------
# Cloud Run service
#
# NOTE: This creates the service definition. The actual container image is
# built and deployed via Cloud Build's GitHub integration (see README).
# On first `terraform apply`, use a placeholder image, then connect your
# GitHub repo in the Cloud Run console to enable continuous deployment.
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "telemetry" {
  name     = "telemetry"
  location = var.region

  template {
    containers {
      image = "gcr.io/cloudrun/placeholder"

      env {
        name  = "TELEMETRY_SECRET"
        value = var.telemetry_secret
      }
      env {
        name  = "BACKUP_BUCKET"
        value = google_storage_bucket.backups.name
      }
      env {
        name  = "FIRESTORE_COLLECTION"
        value = "telemetry_events"
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "256Mi"
        }
      }
    }

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }
  }

  depends_on = [google_project_service.run]
}

# Allow unauthenticated access to the Cloud Run service (the HMAC signature
# provides authentication at the application level).
resource "google_cloud_run_v2_service_iam_member" "public" {
  name     = google_cloud_run_v2_service.telemetry.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# ---------------------------------------------------------------------------
# Service account for Cloud Scheduler
# ---------------------------------------------------------------------------

resource "google_service_account" "scheduler" {
  account_id   = "telemetry-scheduler"
  display_name = "Telemetry Backup Scheduler"
}

resource "google_cloud_run_v2_service_iam_member" "scheduler_invoker" {
  name     = google_cloud_run_v2_service.telemetry.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

# ---------------------------------------------------------------------------
# Cloud Scheduler job — daily backup at 3 AM UTC
# ---------------------------------------------------------------------------

resource "google_cloud_scheduler_job" "daily_backup" {
  name      = "telemetry-daily-backup"
  region    = var.region
  schedule  = "0 3 * * *"
  time_zone = "UTC"

  http_target {
    uri         = "${google_cloud_run_v2_service.telemetry.uri}/backup"
    http_method = "POST"

    headers = {
      "X-Telemetry-Signature" = "" # See README — you must set this after deploy
    }

    oidc_token {
      service_account_email = google_service_account.scheduler.email
    }
  }

  depends_on = [google_project_service.scheduler]
}

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

output "cloud_run_url" {
  value       = google_cloud_run_v2_service.telemetry.uri
  description = "URL of the deployed Cloud Run telemetry service"
}

output "backup_bucket" {
  value       = google_storage_bucket.backups.name
  description = "Cloud Storage bucket for JSONL backups"
}

output "scheduler_signature_command" {
  value       = "python3 -c \"import hmac,hashlib; print(hmac.new(b'YOUR_SECRET', b'backup', hashlib.sha256).hexdigest())\""
  description = "Run this (with your real secret) to get the signature for the scheduler header"
}
