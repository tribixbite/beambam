"""The local assistant router must not act on negated / non-imperative
control phrases.

Audit finding P3: `\\bstop\\b` routed "don't stop the print" (and "why did
it stop?") to the `stop` tool, i.e. aborted the print.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runtime.assistant.router import _local_route  # noqa: E402


@pytest.mark.parametrize("message,tool", [
    ("stop", "stop"),
    ("Stop the print", "stop"),
    ("stop everything", "stop"),
    ("please abort", "stop"),
    ("can you cancel the print?", "stop"),
    ("ok, pause it", "pause"),
    ("pause the print", "pause"),
    ("please resume", "resume"),
    ("Could you please resume printing", "resume"),
    ("home all axes", "home"),
    ("G28", "home"),
    ("level the bed", "level"),
    ("go ahead and run a bed leveling", "level"),
])
def test_imperative_control_routes(message, tool):
    assert _local_route(message) == (tool, {})


@pytest.mark.parametrize("message", [
    "don't stop the print",
    "do not stop",
    "dont pause it",
    "never stop printing",
    "why did it stop?",
    "did it stop?",
    "is it going to stop",
    "what if I stop it",
    "stop the print if it fails",
    "it won't stop beeping",
    "the print stopped",
    "will it pause at layer 20?",
    "I paused it earlier",
    "no, don't resume",
    "stop. no wait, don't",
])
def test_negated_or_descriptive_control_does_not_act(message):
    routed = _local_route(message)
    assert routed is None or routed[0] not in {
        "stop", "pause", "resume", "home", "level"}, (message, routed)


@pytest.mark.parametrize("message,tool", [
    ("what's the chamber temperature?", "status"),
    ("show me the camera", "camera_snapshot"),
    ("list printers please", "list_printers"),
    ("what's in the AMS?", "status"),
    ("what's the AMS humidity level", "status"),   # "level" ≠ bed level
])
def test_read_only_intents(message, tool):
    assert _local_route(message) == (tool, {})
