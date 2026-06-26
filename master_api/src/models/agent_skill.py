"""Pydantic schemas for the `agent-skills/resolve` endpoint.

A skill is sourced from a URI pointing at a `SKILL.md` file with a YAML
frontmatter block (per Anthropic's skills convention). Resolution downloads
the file once, computes its SHA-256, and stores it under
``agent_skills/<sha256>/SKILL.md`` so subsequent ``RUN_AGENT_SKILL`` tasks
re-use the cached copy.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class AgentSkillResolveRequest(BaseModel):
    """Body of ``POST /api/v1/agent-skills/resolve``."""

    uri: str = Field(..., description="HTTP(S) URL of a raw SKILL.md file")
    expected_sha256: Optional[str] = Field(
        None,
        description=(
            "Pin: if provided, the downloaded SKILL.md SHA-256 must match exactly "
            "or the request is rejected with 409."
        ),
    )
    force_refresh: bool = Field(
        False,
        description="When true, re-fetch from the URI even if the URI is in cache.",
    )


class AgentSkillManifest(BaseModel):
    """Parsed YAML frontmatter of a SKILL.md plus the raw mapping."""

    name: str
    description: Optional[str] = None
    allowed_tools: List[str] = Field(default_factory=list)
    raw: Dict[str, Any] = Field(
        default_factory=dict,
        description="Full frontmatter mapping in case callers need fields beyond the typed ones.",
    )


class AgentSkillResolveResponse(BaseModel):
    """Result of resolving and caching a SKILL.md."""

    skill_id: str = Field(..., description="Stable id; equal to ``sha256`` of the SKILL.md bytes.")
    sha256: str
    uri: str
    storage_path: str = Field(..., description="MinIO object name where the SKILL.md was stored.")
    manifest: AgentSkillManifest
    cached: bool = Field(
        ...,
        description="True when the response came from cache without re-fetching the URI.",
    )
    size_bytes: int
