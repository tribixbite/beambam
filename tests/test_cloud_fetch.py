"""Tests for beambam.cloud_fetch.

Most tests mock urlopen so they don't hit the network. The single
live test (auto-tagged via live_printer) does hit MakerWorld + Bambu
Cloud and is skipped without credentials."""
from __future__ import annotations

import json
import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from beambam.cloud_fetch import (
    fetch_design_info,
    list_instances,
    fetch_design_cover,
    format_design_summary,
    format_task_summary,
    format_devices,
)


FAKE_DESIGN = {
    "id": 1501027,
    "title": "Mini Eevee Pokemon",
    "slug": "mini-eevee-pokemon",
    "modelId": "US28b658edb03724",
    "license": "Standard Digital File License",
    "printCount": 840,
    "downloadCount": 1593,
    "likeCount": 622,
    "coverUrl": "https://example.com/cover.png",
    "instances": [
        {
            "id": 1570228,
            "profileId": 322681623,
            "title": "0.2mm layer, 6 walls, 15% infill",
            "extention": {
                "modelInfo": {
                    "compatibility": {"devProductName": "P1S"},
                    "otherCompatibility": [{"devProductName": "X2D"},
                                            {"devProductName": "H2D"}],
                    "plates": [{"prediction": 16537, "weight": 67}],
                },
            },
        },
    ],
}


def _make_response(body: bytes, status: int = 200):
    """Helper: build a mock urlopen response context manager."""
    class _MockResp:
        def __enter__(s):
            return s
        def __exit__(s, *a):
            pass
        def read(s):
            return body
        status = 200
    return _MockResp()


# ---------------------------------------------------------------------------


def test_fetch_design_info_calls_correct_endpoint():
    with patch("urllib.request.urlopen") as urlopen:
        urlopen.return_value = _make_response(json.dumps(FAKE_DESIGN).encode())
        info = fetch_design_info(1501027)
    assert info["id"] == 1501027
    assert info["title"] == "Mini Eevee Pokemon"
    # The endpoint should be the new design-service path, NOT the old design-detail.
    call_url = urlopen.call_args.args[0].full_url
    assert "/api/v1/design-service/design/1501027" in call_url
    assert "design-detail" not in call_url


def test_list_instances():
    with patch("urllib.request.urlopen") as urlopen:
        urlopen.return_value = _make_response(json.dumps(FAKE_DESIGN).encode())
        instances = list_instances(1501027)
    assert len(instances) == 1
    assert instances[0]["profileId"] == 322681623


def test_format_design_summary_includes_key_fields():
    text = format_design_summary(FAKE_DESIGN)
    assert "Mini Eevee" in text
    assert "1501027" in text
    assert "US28b658edb03724" in text
    assert "P1S" in text and "X2D" in text       # compat names listed
    assert "16537s, 67g" in text                 # prediction shown


def test_format_design_summary_handles_no_instances():
    info = {**FAKE_DESIGN, "instances": []}
    text = format_design_summary(info)
    assert "instances:    0" in text


def test_format_design_summary_handles_no_cover():
    info = {**FAKE_DESIGN}
    del info["coverUrl"]
    text = format_design_summary(info)
    assert "cover:" not in text                  # graceful omission


def test_format_task_summary_empty():
    assert "no recent tasks" in format_task_summary([])


def test_format_task_summary_with_tasks():
    tasks = [{"id": 1, "title": "Eevee print", "designId": 1501027,
              "status": "FINISH", "weight": 33.67}]
    text = format_task_summary(tasks)
    assert "1 recent task" in text
    assert "Eevee print" in text


def test_format_devices_empty():
    assert "no printers bound" in format_devices([])


def test_format_devices_with_one_x2d():
    devs = [{"dev_id": "00M09A000000000", "name": "x2d",
             "dev_product_name": "X2D", "dev_access_code": "abcd1234",
             "online": True}]
    text = format_devices(devs)
    assert "x2d" in text
    assert "X2D" in text
    assert "online=True" in text


def test_fetch_design_cover_writes_file(tmp_path):
    """fetch_design_cover should pull the coverUrl bytes and write them."""
    fake_png = b"\x89PNG\r\n\x1a\n" + b"x" * 100
    out = tmp_path / "cover.png"
    with patch("urllib.request.urlopen") as urlopen:
        urlopen.side_effect = [
            _make_response(json.dumps(FAKE_DESIGN).encode()),    # metadata
            _make_response(fake_png),                            # cover bytes
        ]
        n = fetch_design_cover(1501027, out)
    assert n == len(fake_png)
    assert out.read_bytes() == fake_png


# ---- live ----------------------------------------------------------------


@pytest.mark.live
def test_live_fetch_design_info_round_trip(live_printer):
    """Real round-trip to MakerWorld. Uses a stable design (eevee, 2025)."""
    info = fetch_design_info(1501027)
    assert info["modelId"] == "US28b658edb03724"
    assert info.get("instances")
