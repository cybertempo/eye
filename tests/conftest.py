from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = REPO_ROOT / "config" / "eye.example.toml"


@pytest.fixture
def example_raw() -> dict:
    """A fresh parsed copy of the example configuration."""
    with EXAMPLE_CONFIG.open("rb") as handle:
        return tomllib.load(handle)


def dump_toml(raw: dict) -> str:
    """Minimal TOML writer for the flat tables used by EYE configuration."""

    def value(v: object) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, list):
            return "[" + ", ".join(value(i) for i in v) + "]"
        return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'

    lines = [f"{k} = {value(v)}" for k, v in raw.items() if not isinstance(v, dict)]
    for table, entries in raw.items():
        if isinstance(entries, dict):
            lines.append(f"[{table}]")
            lines.extend(f"{k} = {value(v)}" for k, v in entries.items())
    return "\n".join(lines) + "\n"


@pytest.fixture
def write_config(tmp_path: Path):
    """Write a config dict to a temp file with the fixture path made absolute."""

    def _write(raw: dict) -> Path:
        raw = {k: (dict(v) if isinstance(v, dict) else v) for k, v in raw.items()}
        fixture = raw.get("data", {}).get("fixture")
        if fixture and not Path(fixture).is_absolute():
            raw["data"]["fixture"] = str((EXAMPLE_CONFIG.parent / fixture).resolve())
        path = tmp_path / "eye.toml"
        path.write_text(dump_toml(raw), encoding="utf-8")
        return path

    return _write
