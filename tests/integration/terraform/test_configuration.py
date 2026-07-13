# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for Terraform integration test configuration."""

from tests.integration.terraform.test_terraform import (
    _terraform_environment,
    _workspace_main,
)


def test_workspace_forwards_juju_base_to_all_ceph_applications() -> None:
    """The generated root module should override every Ceph application base."""
    workspace = _workspace_main("./terraform")

    assert 'variable "juju_base"' in workspace
    assert workspace.count("var.juju_base == null") == 3
    assert workspace.count("base = var.juju_base") == 3


def test_juju_base_environment_maps_to_terraform_variable(monkeypatch) -> None:
    """JUJU_BASE should be usable without exposing Terraform internals to callers."""
    monkeypatch.setenv("JUJU_BASE", "ubuntu@26.04")
    monkeypatch.delenv("TF_VAR_juju_base", raising=False)

    env = _terraform_environment("controller:model")

    assert env["TF_VAR_juju_base"] == "ubuntu@26.04"


def test_explicit_terraform_juju_base_takes_precedence(monkeypatch) -> None:
    """An explicit Terraform variable should take precedence over the convenience input."""
    monkeypatch.setenv("JUJU_BASE", "ubuntu@26.04")
    monkeypatch.setenv("TF_VAR_juju_base", "ubuntu@24.04")

    env = _terraform_environment("controller:model")

    assert env["TF_VAR_juju_base"] == "ubuntu@24.04"
