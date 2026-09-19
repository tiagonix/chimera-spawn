"""
Tests for container models.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

import pytest
from pydantic import ValidationError

from chimera.models.container import CloudInitSpec, ContainerRecord, ContainerSpec


class TestContainerSpec:
    """Test ContainerSpec model."""

    def test_minimal_container_spec(self):
        """Test creating container spec with minimal fields."""
        spec = ContainerSpec(name="test-container", image="ubuntu-24.04-cloud-tar")

        assert spec.name == "test-container"
        assert spec.image == "ubuntu-24.04-cloud-tar"
        assert spec.ensure == "present"
        assert spec.state == "running"
        assert spec.autostart is True
        assert spec.cloud_init is None

    def test_full_container_spec(self):
        """Test creating container spec with all fields."""
        cloud_init = CloudInitSpec(
            meta_data={"instance-id": "test-123"}, user_data="#cloud-config\nusers: []"
        )

        spec = ContainerSpec(
            name="test-container",
            ensure="absent",
            state="stopped",
            image="debian-12-cloud-raw",
            profile="privileged",
            cloud_init=cloud_init,
            autostart=False,
        )

        assert spec.name == "test-container"
        assert spec.ensure == "absent"
        assert spec.state == "stopped"
        assert spec.image == "debian-12-cloud-raw"
        assert spec.profile == "privileged"
        assert spec.autostart is False
        assert spec.cloud_init.meta_data["instance-id"] == "test-123"

    def test_invalid_ensure_value(self):
        """Test validation of ensure field."""
        with pytest.raises(ValidationError) as exc_info:
            ContainerSpec(name="test", image="test-image", ensure="maybe")  # Invalid value

        assert "ensure" in str(exc_info.value)

    def test_invalid_state_value(self):
        """Test validation of state field."""
        with pytest.raises(ValidationError) as exc_info:
            ContainerSpec(name="test", image="test-image", state="paused")  # Invalid value

        assert "state" in str(exc_info.value)

    @pytest.mark.parametrize("name", ["../outside", "has/slash", "..", "-leading"])
    def test_rejects_unsafe_machine_names(self, name):
        """Container names become file and unit names, so they must be safe."""
        with pytest.raises(ValidationError):
            ContainerSpec(name=name, image="ubuntu")

    def test_complete_record_requires_materialization_binding(self):
        """Completed provisioning always identifies the initialized materialization."""
        with pytest.raises(ValidationError, match="materialization_id"):
            ContainerRecord(
                spec=ContainerSpec(name="test-container", image="ubuntu"),
                provisioning_state="complete",
            )


class TestCloudInitSpec:
    """Test CloudInitSpec model."""

    def test_empty_cloud_init(self):
        """Test creating empty cloud-init spec."""
        spec = CloudInitSpec()

        assert spec.meta_data == {}
        assert spec.user_data is None
        assert spec.network_config is None
        assert spec.template is None

    def test_cloud_init_with_template(self):
        """Test cloud-init spec with template reference."""
        spec = CloudInitSpec(template="ubuntu_base", meta_data={"custom": "value"})

        assert spec.template == "ubuntu_base"
        assert spec.meta_data["custom"] == "value"

    def test_cloud_init_extra_fields(self):
        """Test that cloud-init allows extra fields for merging."""
        spec = CloudInitSpec(
            template="base", extra_field="extra_value", another_field={"nested": "value"}
        )

        # Should not raise validation error
        assert hasattr(spec, "extra_field")
        assert spec.extra_field == "extra_value"
