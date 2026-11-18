from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import yaml

from openmail_plugin.__init__ import register


def test_manifest_declares_every_registered_tool():
    """Hermes admission rejects tools registered without a manifest declaration."""
    ctx = SimpleNamespace(register_platform=Mock(), register_tool=Mock(), register_skill=Mock())
    register(ctx)

    manifest_path = Path(__file__).resolve().parents[1] / "plugin.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    registered = {call.args[0] for call in ctx.register_tool.call_args_list}
    assert registered
    assert set(manifest["provides_tools"]) == registered
