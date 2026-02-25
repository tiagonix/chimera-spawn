"""Tests for the StateEngine."""

import pytest
from unittest.mock import Mock

from chimera.agent.engine import StateEngine
from chimera.models.container import ContainerSpec, CloudInitSpec


@pytest.fixture
def mock_config_manager():
    """Create a mock ConfigManager with pre-loaded data."""
    manager = Mock()
    manager.cloud_init_templates = {
        "base_template": {
            "meta_data": {"base_key": "base_value", "nested": {"a": 1}},
            "user_data": "base user data",
        }
    }
    return manager


@pytest.fixture
def state_engine(mock_config_manager):
    """Create a StateEngine instance with mocked dependencies."""
    # Provider registry is not needed for this specific test
    return StateEngine(config_manager=mock_config_manager, provider_registry=Mock())


class TestStateEngineEnrichment:
    """Test the enrichment logic within the StateEngine."""

    def test_enrich_cloud_init_spec_deep_merge(self, state_engine):
        """Test that cloud-init specs are correctly deep-merged."""
        container_spec = ContainerSpec(
            name="test-container",
            image="test-image",
            cloud_init=CloudInitSpec(
                template="base_template",
                meta_data={"override_key": "override_value", "nested": {"b": 2}},
            ),
        )

        # The method to be tested
        state_engine._enrich_cloud_init_spec(container_spec)

        # Assertions
        enriched_ci = container_spec.cloud_init
        assert enriched_ci is not None

        # Verify deep merge of meta_data
        meta = enriched_ci.meta_data
        assert meta["base_key"] == "base_value"
        assert meta["override_key"] == "override_value"
        assert meta["nested"] == {"a": 1, "b": 2}

        # Verify base user_data is used as override is absent
        assert enriched_ci.user_data == "base user data"

        # Verify template key is removed after processing
        assert enriched_ci.template is None

    def test_enrich_cloud_init_no_template(self, state_engine):
        """Test that enrichment does nothing if no template is specified."""
        original_cloud_init = CloudInitSpec(meta_data={"key": "value"})
        container_spec = ContainerSpec(
            name="test-container", image="test-image", cloud_init=original_cloud_init
        )

        state_engine._enrich_cloud_init_spec(container_spec)

        # The spec should remain unchanged
        assert container_spec.cloud_init == original_cloud_init

    def test_enrich_cloud_init_no_cloud_init_spec(self, state_engine):
        """Test that enrichment handles cases with no cloud_init spec gracefully."""
        container_spec = ContainerSpec(name="test-container", image="test-image")

        # Should not raise an error
        state_engine._enrich_cloud_init_spec(container_spec)

        assert container_spec.cloud_init is None
