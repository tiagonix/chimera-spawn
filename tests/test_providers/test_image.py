"""Safety contracts for image provider observation and catalog semantics."""

from unittest.mock import AsyncMock, patch

import pytest

from chimera.errors import ChimeraError
from chimera.models.image import CustomFileSpec, ImageSpec
from chimera.providers.base import ProviderStatus
from chimera.providers.image import ImageProvider


@pytest.mark.asyncio
async def test_image_observation_error_cannot_trigger_pull(tmp_path):
    """An unavailable machinectl result must not be treated as a missing image."""
    provider = ImageProvider()
    provider.machines_dir = tmp_path
    image = ImageSpec(name="base", type="tar", source="https://example.invalid/base.tar")
    provider.status = AsyncMock(return_value=ProviderStatus.ERROR)

    with (
        patch("chimera.providers.image.run_command", new_callable=AsyncMock) as run_command,
        pytest.raises(ChimeraError, match="Could not determine"),
    ):
        await provider.present(image)

    run_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_raw_image_custom_files_are_rejected():
    """Raw-image catalogs cannot claim unsupported in-place customizations."""
    provider = ImageProvider()
    raw_image = ImageSpec(
        name="raw",
        type="raw",
        source="https://example.invalid/base.raw",
        custom_files=[CustomFileSpec(path="etc/example", ensure="absent")],
    )

    with pytest.raises(ChimeraError, match="cannot use custom_files"):
        await provider.validate_spec(raw_image)
