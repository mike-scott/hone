"""The release version (common/version.py) and how each tier surfaces it."""
import re

from common.version import __version__
from node.runner import _print_banner


def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__), __version__


def test_node_startup_banner_carries_the_version(capsys):
    _print_banner()
    assert f"hone-node-{__version__}" in capsys.readouterr().out
