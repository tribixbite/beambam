"""tests/test_runtime_subprocesses.py — wrap runtime/* standalone tests
into pytest collection.

The `runtime/{ha,mcp,webui,timelapse,queue,colorsync,assistant,webrtc,
network_shim}/test_*.py` files are script-style end-to-end tests that
print `PASS  …` / `FAIL  …` lines and exit 0 on success, non-zero on
failure. They were never converted to assert-style pytest functions
because each test internally drives a complex thread/server/process
fixture, and porting all 17 was bigger than this release's scope.

To get them under CI coverage anyway, this file parametrizes over the
discovered scripts and spawns each as a subprocess (with the repo root
on PYTHONPATH so `from runtime.X import Y` works). Exit 0 = pass; any
non-zero exit + stderr surfaces as a pytest failure with the captured
output attached.

Per-script timeout is 60 s — most of these set up sockets + threads and
finish in <2 s, but a few (timelapse stitch, mcp e2e) hit ffmpeg or the
real bridge and need longer.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = REPO_ROOT / "runtime"


def _discover_runtime_tests() -> list[Path]:
    """Walk runtime/ for every `test_*.py` file. Returns sorted paths
    (relative to repo root) so the parametrized test IDs are stable
    across runs."""
    if not RUNTIME_DIR.is_dir():
        return []
    return sorted(RUNTIME_DIR.rglob("test_*.py"))


_TESTS = _discover_runtime_tests()


@pytest.fixture(scope="session")
def runtime_env():
    """Build the environment dict every subprocess test inherits.
    `PYTHONPATH` includes the repo root so `from runtime.X import Y`
    resolves regardless of where pytest was invoked from."""
    env = os.environ.copy()
    repo = str(REPO_ROOT)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{repo}{os.pathsep}{existing}" if existing else repo
    return env


# Per-script timeout overrides for the slower ones. Default 60 s.
_TIMEOUTS_S: dict[str, float] = {
    "runtime/timelapse/test_recorder.py": 120.0,   # ffmpeg stitch
    "runtime/timelapse/test_http.py":     120.0,
    "runtime/mcp/test_mcp.py":             90.0,   # spawns the real bridge
}


# Scripts to skip even from the subprocess wrapper. Mostly things that
# need a real network reachable target (Bambu Cloud, an actual printer)
# or a binary we don't ship in CI.
_SKIP_REASONS: dict[str, str] = {
    # WebRTC end-to-end needs aiortc + a working ICE path; aiortc isn't
    # in the CI test deps and the test does real codec setup.
    "runtime/webrtc/test_webrtc.py":
        "aiortc + WebRTC ICE — not in CI test deps",
    # Live MCP client connects to an actual running daemon on :8765;
    # CI doesn't have one.
    "runtime/mcp/test_live_client.py":
        "needs a live daemon on http://127.0.0.1:8765",
    # network_shim e2e dlopens libbambu_networking.so which is only
    # built locally; CI doesn't build it.
    "runtime/network_shim/tests/test_shim_e2e.py":
        "needs the locally-built libbambu_networking.so",
    # Phase 2 smoke spawns the bridge + drives a print job through it;
    # heavy + flaky in CI.
    "runtime/test_phase2_smoke.py":
        "drives a full print job end-to-end — too heavy for CI",
}


@pytest.mark.parametrize(
    "test_path",
    _TESTS,
    ids=[str(p.relative_to(REPO_ROOT)) for p in _TESTS],
)
def test_runtime_script(test_path: Path, runtime_env: dict[str, str]):
    """Spawn `python <runtime test>` and assert exit 0.

    A non-zero exit code attaches the captured stderr to the pytest
    failure message so the user can diagnose without re-running."""
    rel = test_path.relative_to(REPO_ROOT).as_posix()
    if rel in _SKIP_REASONS:
        pytest.skip(_SKIP_REASONS[rel])

    timeout = _TIMEOUTS_S.get(rel, 60.0)
    try:
        result = subprocess.run(
            [sys.executable, str(test_path)],
            cwd=str(REPO_ROOT),
            env=runtime_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        pytest.fail(
            f"{rel} exceeded {timeout}s timeout\n"
            f"stdout tail:\n{(e.stdout or '')[-2000:]}\n"
            f"stderr tail:\n{(e.stderr or '')[-2000:]}")

    if result.returncode != 0:
        pytest.fail(
            f"{rel} exited {result.returncode}\n"
            f"stdout tail:\n{result.stdout[-4000:]}\n"
            f"stderr tail:\n{result.stderr[-2000:]}")


def test_discovery_found_known_runtime_tests():
    """Sanity: discovery must have found the established suite. If this
    drops below 10 we either lost some test files or pytest's collection
    rules diverged from rglob's."""
    rels = [p.relative_to(REPO_ROOT).as_posix() for p in _TESTS]
    assert len(rels) >= 10, (
        f"runtime tests discovery returned only {len(rels)} files — "
        f"expected ≥10; got {rels}")
    # Spot-check that representative files are picked up.
    for must_have in (
        "runtime/queue/test_queue.py",
        "runtime/ha/test_ha.py",
        "runtime/colorsync/test_mapper.py",
    ):
        assert must_have in rels, f"discovery missed {must_have}"
