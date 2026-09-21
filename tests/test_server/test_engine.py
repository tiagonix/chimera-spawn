"""
Tests for the StateEngine.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

from unittest.mock import Mock

import pytest

from chimera.server.engine import StateEngine
from chimera.server.store import ContainerStore
from chimera.models.container import CloudInitSpec, ContainerSpec


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
def state_engine(mock_config_manager, tmp_path):
    """Create a StateEngine instance with mocked dependencies."""
    # Provider registry is not needed for this specific test
    return StateEngine(
        config_manager=mock_config_manager,
        provider_registry=Mock(),
        store=ContainerStore(tmp_path / "state.json"),
    )


class TestStateEngineEnrichment:
    """Test the enrichment logic within the StateEngine."""

    def test_enrich_cloud_init_spec_deep_merge(self, state_engine):
        """Test that cloud-init specs are correctly deep-merged."""
        container_spec = ContainerSpec(
            name="test-container",
            image="test-image",
            image_source="ubuntu",
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
