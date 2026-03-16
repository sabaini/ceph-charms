# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

locals {
  normalized_model_uuid = var.model_uuid == null ? null : trimspace(var.model_uuid)
  normalized_model_name = var.model_name == null ? null : trimspace(var.model_name)

  has_model_uuid = local.normalized_model_uuid != null && local.normalized_model_uuid != ""
  has_model_name = local.normalized_model_name != null && local.normalized_model_name != ""

  model_target_valid = local.has_model_uuid != local.has_model_name

  # Resolve model UUID either directly from input or via name/owner lookup.
  use_model_lookup = !local.has_model_uuid && local.has_model_name
}

# Enforce explicit model targeting at plan time with a clear error message.
resource "terraform_data" "validate_model_target" {
  count = local.model_target_valid ? 0 : 1
  input = true

  lifecycle {
    precondition {
      condition     = local.model_target_valid
      error_message = "Exactly one model target must be provided: set either non-empty model_uuid or non-empty model_name (optionally with model_owner)."
    }
  }
}

data "juju_model" "target" {
  count = local.use_model_lookup ? 1 : 0
  name  = local.normalized_model_name
  owner = var.model_owner
}

locals {
  # Keep downstream model_uuid arguments non-null for validation failures.
  # The precondition above ensures this fallback never applies to successful plans.
  resolved_model_uuid = coalesce(
    local.has_model_uuid ? local.normalized_model_uuid : null,
    try(data.juju_model.target[0].uuid, null),
    "00000000-0000-0000-0000-000000000000",
  )
}
