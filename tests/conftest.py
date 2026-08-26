from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2]))


class FakeState:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir


class FakeContext:
    def __init__(self, data_dir: Path, settings: dict | None = None) -> None:
        self.state = FakeState(data_dir)
        self.settings = settings or {}
        self.tools = []
        self.hooks = []

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)

    def register_hook(self, name, callback):
        self.hooks.append((name, callback))


@pytest.fixture
def fake_ctx(tmp_path):
    return FakeContext(tmp_path / "plugin-data")
