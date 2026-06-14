"""HTTP endpoints for the AgentSkill resolver."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException

from ...models.agent_skill import AgentSkillResolveRequest, AgentSkillResolveResponse
from ...services.agent_skill_resolver import (
    AgentSkillError,
    AgentSkillFetchError,
    AgentSkillManifestError,
    AgentSkillResolver,
    AgentSkillShaMismatchError,
)
from ...services.service_manager import ServiceManager

router = APIRouter()

_service_manager: Optional[ServiceManager] = None


def set_service_manager(service_manager: ServiceManager) -> None:
    global _service_manager
    _service_manager = service_manager


def _get_resolver() -> AgentSkillResolver:
    if _service_manager is None:
        raise HTTPException(status_code=503, detail="Service manager not initialized")
    return AgentSkillResolver(storage=_service_manager.get_storage_service())


@router.post("/resolve", response_model=AgentSkillResolveResponse)
async def resolve_agent_skill(req: AgentSkillResolveRequest) -> AgentSkillResolveResponse:
    """Download, verify, parse and cache a SKILL.md.

    Status mapping for failures:

    - ``409`` — ``expected_sha256`` pin mismatch (body contains both hashes).
    - ``422`` — SKILL.md frontmatter missing or malformed.
    - ``502`` — URI could not be fetched.
    """
    resolver = _get_resolver()
    try:
        return await resolver.resolve(req)
    except AgentSkillShaMismatchError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "sha256_mismatch", "expected": exc.expected, "actual": exc.actual},
        ) from exc
    except AgentSkillManifestError as exc:
        raise HTTPException(status_code=422, detail={"error": "invalid_skill_md", "message": str(exc)}) from exc
    except AgentSkillFetchError as exc:
        raise HTTPException(status_code=502, detail={"error": "fetch_failed", "message": str(exc)}) from exc
    except AgentSkillError as exc:
        raise HTTPException(status_code=500, detail={"error": "resolver_error", "message": str(exc)}) from exc
