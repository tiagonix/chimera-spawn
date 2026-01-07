"""Tests for image models."""

import pytest
from pydantic import ValidationError

from chimera.models.image import ImageSpec, CustomFileSpec


class TestImageSpec:
    """Test ImageSpec model."""
    
    def test_minimal_image_spec(self):
        """Test creating image spec with minimal fields."""
        spec = ImageSpec(
            name="test-image",
            type="tar",
            source="https://example.com/image.tar.xz"
        )
        
        assert spec.name == "test-image"
        assert spec.type == "tar"
        assert spec.source == "https://example.com/image.tar.xz"
        assert spec.verify == "signature"
        assert spec.custom_files == []
        
    def test_full_image_spec(self):
        """Test creating image spec with all fields."""
        custom_file = CustomFileSpec(
            path="etc/fstab",
            ensure="absent"
        )
        
        spec = ImageSpec(
            name="test-image",
            type="raw",
            verify="no",
            source="https://example.com/image.qcow2",
            custom_files=[custom_file]
        )
        
        assert spec.name == "test-image"
        assert spec.type == "raw"
        assert spec.verify == "no"
        assert len(spec.custom_files) == 1
        assert spec.custom_files[0].path == "etc/fstab"
        
    def test_invalid_image_type(self):
        """Test validation of image type."""
        with pytest.raises(ValidationError) as exc_info:
            ImageSpec(
                name="test",
                type="invalid",  # Invalid type
                source="https://example.com/image"
            )
            
        assert "type" in str(exc_info.value)


class TestCustomFileSpec:
    """Test CustomFileSpec model."""
    
    def test_file_removal(self):
        """Test file removal spec."""
        spec = CustomFileSpec(
            path="etc/fstab",
            ensure="absent"
        )
        
        assert spec.path == "etc/fstab"
        assert spec.ensure == "absent"
        assert spec.target is None
        
    def test_symlink_creation(self):
        """Test symlink creation spec."""
        spec = CustomFileSpec(
            path="etc/systemd/system/test.service",
            ensure="link",
            target="/dev/null"
        )
        
        assert spec.path == "etc/systemd/system/test.service"
        assert spec.ensure == "link"
        assert spec.target == "/dev/null"
        
    def test_invalid_ensure_value(self):
        """Test validation of ensure field."""
        with pytest.raises(ValidationError) as exc_info:
            CustomFileSpec(
                path="test",
                ensure="maybe"  # Invalid value
            )
            
        assert "ensure" in str(exc_info.value)
