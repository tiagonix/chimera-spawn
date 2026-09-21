"""
Tests for container models.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

import pytest
from pydantic import ValidationError

from chimera.models.container import CloudInitSpec, ContainerRecord, ContainerSpec
from chimera.pydantic_compat import model_validate


class TestContainerSpec:
    """Test ContainerSpec model."""

    def test_minimal_container_spec(self):
        """Test creating container spec with minimal fields."""
        spec = ContainerSpec(
            name="test-container",
            image="com.ubuntu.cloud:server:24.04:amd64",
            image_source="ubuntu",
        )

        assert spec.name == "test-container"
        assert spec.image == "com.ubuntu.cloud:server:24.04:amd64"
        assert spec.image_source == "ubuntu"
        assert spec.image_artifact == "rootfs"
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
            image="com.ubuntu.cloud:server:24.04:amd64",
            image_source="ubuntu",
            image_artifact="disk",
            profile="privileged",
            cloud_init=cloud_init,
            autostart=False,
        )

        assert spec.name == "test-container"
        assert spec.ensure == "absent"
        assert spec.state == "stopped"
        assert spec.image == "com.ubuntu.cloud:server:24.04:amd64"
        assert spec.image_source == "ubuntu"
        assert spec.image_artifact == "disk"
        assert spec.profile == "privileged"
        assert spec.autostart is False
        assert spec.cloud_init.meta_data["instance-id"] == "test-123"

    def test_invalid_ensure_value(self):
        """Test validation of ensure field."""
        with pytest.raises(ValidationError) as exc_info:
            ContainerSpec(
                name="test", image="test-image", image_source="ubuntu", ensure="maybe"
            )  # Invalid value

        assert "ensure" in str(exc_info.value)

    def test_invalid_state_value(self):
        """Test validation of state field."""
        with pytest.raises(ValidationError) as exc_info:
            ContainerSpec(
                name="test", image="test-image", image_source="ubuntu", state="paused"
            )  # Invalid value

        assert "state" in str(exc_info.value)

    @pytest.mark.parametrize(
        "name", ["../outside", "has/slash", "..", "-leading", "chimera-src-demo"]
    )
    def test_rejects_unsafe_machine_names(self, name):
        """Container names become file and unit names, so they must be safe."""
        with pytest.raises(ValidationError):
            ContainerSpec(name=name, image="ubuntu", image_source="ubuntu")

    def test_complete_record_requires_materialization_binding(self):
        """Completed provisioning always identifies the initialized materialization."""
        with pytest.raises(ValidationError, match="materialization_id"):
            ContainerRecord(
                spec=ContainerSpec(
                    name="test-container",
                    image="com.ubuntu.cloud:server:24.04:amd64",
                    image_source="ubuntu",
                ),
                provisioning_state="complete",
            )

    def test_missing_image_source_is_rejected(self):
        """Durable records require a configured SimpleStreams source name."""
        with pytest.raises(ValidationError):
            model_validate(
                ContainerRecord,
                {
                    "schema_version": 1,
                    "spec": {"name": "demo", "image": "com.ubuntu.cloud:server:24.04:amd64"},
                    "desired_state": "stopped",
                },
            )

    def test_rootfs_and_disk_are_different_create_intent(self):
        """The same product rootfs and disk are distinct creation identities."""
        rootfs = ContainerRecord(
            spec=ContainerSpec(
                name="demo",
                image="com.ubuntu.cloud:server:26.04:amd64",
                image_source="ubuntu",
                image_artifact="rootfs",
            )
        )
        disk = ContainerRecord(
            spec=ContainerSpec(
                name="demo",
                image="com.ubuntu.cloud:server:26.04:amd64",
                image_source="ubuntu",
                image_artifact="disk",
            )
        )
        assert rootfs.creation_identity() != disk.creation_identity()

    def test_source_changes_create_identity(self):
        """The same product from different sources is distinct creation identity."""
        ubuntu = ContainerRecord(
            spec=ContainerSpec(
                name="demo",
                image="com.ubuntu.cloud:server:26.04:amd64",
                image_source="ubuntu",
            )
        )
        company = ContainerRecord(
            spec=ContainerSpec(
                name="demo",
                image="com.ubuntu.cloud:server:26.04:amd64",
                image_source="company",
            )
        )
        assert ubuntu.creation_identity() != company.creation_identity()


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
