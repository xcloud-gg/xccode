"""Repository markers (§4.4): `.xccode-secret` refuses, `.xccode-internal` stays local-only."""

from __future__ import annotations

from enum import Enum
from pathlib import Path


class RepoPolicy(Enum):
    NORMAL = "normal"
    INTERNAL = "internal"  # internal repository; rules decide, flagged in the routing event
    SECRET = "secret"  # refused; xccode has no local model for SECRET work (§13)


_RANK = {RepoPolicy.NORMAL: 0, RepoPolicy.INTERNAL: 1, RepoPolicy.SECRET: 2}


def repo_policy(
    cwd: Path | str | None,
    remote_url: str | None = None,
    overrides: dict[str, RepoPolicy] | None = None,
) -> RepoPolicy:
    """The strictest of: markers in cwd or any parent, and the operator's override by remote URL.

    A missing or relative cwd is treated as INTERNAL, never as NORMAL: unknown is not safe to send.
    """
    found = RepoPolicy.NORMAL
    if overrides and remote_url and remote_url in overrides:
        found = overrides[remote_url]
    if cwd is None or not Path(cwd).is_absolute():
        return max(found, RepoPolicy.INTERNAL, key=_RANK.__getitem__)
    p = Path(cwd)
    for d in (p, *p.parents):
        if (d / ".xccode-secret").exists():
            return RepoPolicy.SECRET
        if (d / ".xccode-internal").exists():
            found = max(found, RepoPolicy.INTERNAL, key=_RANK.__getitem__)
    return found
