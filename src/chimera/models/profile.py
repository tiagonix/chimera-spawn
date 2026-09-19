"""
Profile specification models.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

from pydantic import Field

from chimera.pydantic_compat import IgnoreExtraModel


class ProfileSpec(IgnoreExtraModel):
    """nspawn profile specification."""

    name: str = Field(..., description="Profile name")
    description: str | None = Field(
        default=None, description="Optional human-readable profile summary"
    )
    nspawn_config_content: str = Field(..., description="Content for .nspawn file")
    systemd_override_content: str = Field(..., description="Systemd service override content")
