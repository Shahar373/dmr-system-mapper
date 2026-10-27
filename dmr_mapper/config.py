"""Configuration loading: config.example.yaml deep-merged with config.local.yaml."""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

EXAMPLE_NAME = "config.example.yaml"
LOCAL_NAME = "config.local.yaml"
ENV_VAR = "DMR_MAPPER_CONFIG"


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Return a new dict: ``override`` merged into ``base`` recursively (lists are replaced)."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    return data


def load_config(explicit: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Load configuration.

    Order: the example file (always, as defaults), then one override file: ``explicit``,
    else ``$DMR_MAPPER_CONFIG``, else ``./config.local.yaml`` (cwd, then repo root) if present.
    """
    root = repo_root()
    example = root / EXAMPLE_NAME
    cfg = _load_yaml(example) if example.exists() else {}

    override: Path | None = None
    if explicit:
        override = Path(explicit)
        if not override.exists():
            raise FileNotFoundError(override)
    elif os.environ.get(ENV_VAR):
        override = Path(os.environ[ENV_VAR])
        if not override.exists():
            raise FileNotFoundError(f"${ENV_VAR} points to a missing file: {override}")
    else:
        for candidate in (Path.cwd() / LOCAL_NAME, root / LOCAL_NAME):
            if candidate.exists():
                override = candidate
                break
    if override is not None and override.exists():
        cfg = deep_merge(cfg, _load_yaml(override))
    cfg.setdefault("_source", str(override) if override else str(example))
    return cfg


def resolve_path(cfg: dict[str, Any], key: str) -> Path:
    """Resolve ``paths.<key>`` relative to the repository root unless absolute."""
    raw = Path(cfg.get("paths", {}).get(key, f"var/{key}"))
    return raw if raw.is_absolute() else repo_root() / raw
