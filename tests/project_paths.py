"""Explicit project-root context for tests that audit the host checkout."""
from __future__ import annotations

import os
from pathlib import Path


ENV_NAME = 'AGENTBUS_TEST_PROJECT_ROOT'
SOURCE_ENV_NAME = 'AGENTBUS_TEST_SOURCE_ROOT'


def host_project_root() -> Path | None:
    """Return the explicitly selected host project, or None in portable runs.

    Never infer the project by walking parent directories: release tests run from
    an allowlisted temporary snapshot whose ancestors are not the host project.
    """
    raw = os.environ.get(ENV_NAME)
    if raw is None or not raw.strip():
        return None
    root = Path(raw).expanduser().resolve()
    if not root.is_dir() or not (root / '.agentbus' / 'config.json').is_file():
        raise ValueError(f'{ENV_NAME} must name a project containing .agentbus/config.json')
    return root


def host_source_root() -> Path | None:
    """Explicit development source context; never walk snapshot ancestors."""
    raw = os.environ.get(SOURCE_ENV_NAME)
    if raw is None or not raw.strip():
        return None
    if any(p.casefold() == '.workbuddy' for p in raw.replace('\\', '/').split('/')):
        raise ValueError('Private host directories cannot be test source roots')
    root = Path(raw).expanduser().resolve()
    if (not (root / 'agentbus/__init__.py').is_file()
            or not (root / 'tools').is_dir() or not (root / 'tests').is_dir()):
        raise ValueError(f'{SOURCE_ENV_NAME} must name an explicit AgentBus development source root')
    return root
