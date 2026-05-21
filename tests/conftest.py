"""Load the hyphen-named plugin dir as ``hermes_plugins.signal_group_chat``.

Mirrors the Hermes plugin loader so tests can ``from hermes_plugins.signal_group_chat...``
import the package despite the directory name containing a hyphen. Requires the
Hermes source on PYTHONPATH (run inside the container / hermes venv).
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

PKG_DIR = Path(__file__).resolve().parent.parent
_NS = "hermes_plugins"
_NAME = "hermes_plugins.signal_group_chat"


def _ensure_loaded():
    if _NAME in sys.modules:
        return
    if _NS not in sys.modules:
        ns = types.ModuleType(_NS)
        ns.__path__ = []
        ns.__package__ = _NS
        sys.modules[_NS] = ns
    spec = importlib.util.spec_from_file_location(
        _NAME, str(PKG_DIR / "__init__.py"), submodule_search_locations=[str(PKG_DIR)]
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = _NAME
    mod.__path__ = [str(PKG_DIR)]
    sys.modules[_NAME] = mod
    spec.loader.exec_module(mod)


_ensure_loaded()


@pytest.fixture()
def store(tmp_path):
    from hermes_plugins.signal_group_chat.config_store import ConfigStore

    return ConfigStore(path=tmp_path / "store.json")


@pytest.fixture()
def modes(store):
    from hermes_plugins.signal_group_chat.modes import ModeManager

    return ModeManager(store)
