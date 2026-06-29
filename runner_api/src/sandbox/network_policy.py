"""Translate SKILL.md ``allowed-tools`` entries into a sandbox network allowlist.

Anthropic's skills convention puts network requirements inside the
``allowed-tools`` frontmatter as tokens like ``WebFetch(domain:example.com)``
or ``WebFetch(domain:*.adobe.com)``. The sandbox cares only about the
host portions; this module extracts and normalises them.
"""

from __future__ import annotations

import re
from typing import Iterable, List

from .protocol import NetworkPolicy

__all__ = ["NetworkPolicy", "extract_allowed_domains"]

# Match a single WebFetch(...) token, capturing the domain portion. We accept
# both ``WebFetch(domain:foo.com)`` and ``WebFetch(domain: foo.com)`` (with
# whitespace). The host MUST be followed by an immediate closing paren to
# reject smuggled-arg cases like ``WebFetch(domain:rm -rf /)``.
_WEBFETCH_RE = re.compile(
    r"^WebFetch\(\s*domain\s*:\s*(?P<host>[^)\s,]+)\s*\)\s*$",
    re.IGNORECASE,
)


def _is_valid_host(host: str) -> bool:
    """Accept hostnames + wildcards like ``*.example.com``; reject obvious junk.

    We're not trying to be a full RFC validator — just keep callers from
    feeding ``rm -rf /`` to a downstream allowlist file.
    """
    if not host or len(host) > 253:
        return False
    if host in {"*", "."}:
        return False
    # Allowed: letters, digits, hyphen, dot, asterisk (for wildcard subdomains).
    return bool(re.fullmatch(r"[A-Za-z0-9.\-*]+", host))


def extract_allowed_domains(allowed_tools: Iterable[str]) -> List[str]:
    """Pull the unique, normalised domain list out of SKILL.md ``allowed-tools``.

    Returns hosts in the order they first appeared (stable for snapshot tests).
    Invalid / non-WebFetch entries are silently dropped.
    """
    seen: List[str] = []
    seen_set: set[str] = set()
    for entry in allowed_tools:
        if not isinstance(entry, str):
            continue
        match = _WEBFETCH_RE.match(entry.strip())
        if not match:
            continue
        host = match.group("host").strip().lower()
        if not _is_valid_host(host):
            continue
        if host in seen_set:
            continue
        seen_set.add(host)
        seen.append(host)
    return seen
