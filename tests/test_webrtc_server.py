"""WebRTC gateway regressions (runtime/webrtc/server.py).

Audit finding P2:
* the MJPEG decode handler caught `av.AVError`, which PyAV 17 does not
  have: the first undecodable frame raised AttributeError and killed the
  track instead of emitting a black frame;
* the gateway had no Bearer auth (the daemon has), and no request guard;
* peers that never connected stayed in the "new" state forever.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("aiortc")
pytest.importorskip("aiohttp")

import av  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from aiortc import RTCPeerConnection  # noqa: E402

from runtime.webrtc import server as W  # noqa: E402


def test_decode_errors_use_existing_pyav_class():
    assert ValueError in W._AV_DECODE_ERRORS
    assert av.FFmpegError in W._AV_DECODE_ERRORS
    assert all(isinstance(e, type) for e in W._AV_DECODE_ERRORS)


def test_bad_jpeg_yields_black_frame_not_exception():
    async def run():
        store = W._LatestFrameStore()
        track = W.CameraVideoTrack(store, frame_hz=10)
        await store.put(b"\xff\xd8\xff" + b"\x00" * 200)   # corrupt JPEG
        frame = await asyncio.wait_for(track.recv(), timeout=5)
        return frame.width, frame.height
    assert asyncio.run(run()) == (320, 240)


async def _client(**kw) -> tuple[TestClient, W.WebRTCServer]:
    srv = W.WebRTCServer(camera_url="http://127.0.0.1:1", stun_servers=[],
                         **kw)
    client = TestClient(TestServer(srv.make_app(), host="127.0.0.1"))
    await client.start_server()
    return client, srv


def test_token_required_for_data_routes():
    async def run():
        client, srv = await _client(auth_token="tok")
        try:
            out = {}
            out["health_none"] = (await client.get("/healthz")).status
            out["health_bad"] = (await client.get(
                "/healthz", headers={"Authorization": "Bearer nope"})).status
            # 503 = authorised but no frame yet (no camera in this test).
            out["health_bearer"] = (await client.get(
                "/healthz", headers={"Authorization": "Bearer tok"})).status
            out["health_cookie"] = (await client.get(
                "/healthz", headers={"Cookie": "x2d_token=tok"})).status
            out["jpg_none"] = (await client.get("/cam.jpg")).status
            out["offer_none"] = (await client.post(
                "/cam.webrtc/offer", json={"sdp": "x", "type": "offer"})).status
            out["page"] = (await client.get("/cam.webrtc.html")).status
            return out
        finally:
            await client.close()
            await srv.stop()
    out = asyncio.run(run())
    assert out["health_none"] == 401
    assert out["health_bad"] == 401
    assert out["health_bearer"] == 503
    assert out["health_cookie"] == 503
    assert out["jpg_none"] == 401
    assert out["offer_none"] == 401
    assert out["page"] == 200


def test_no_token_open_on_loopback_closed_otherwise():
    async def run(bind_host: str) -> int:
        client, srv = await _client(bind_host=bind_host)
        try:
            return (await client.get("/healthz")).status
        finally:
            await client.close()
            await srv.stop()
    assert asyncio.run(run("127.0.0.1")) == 503
    assert asyncio.run(run("0.0.0.0")) == 401


def test_cross_site_offer_is_refused():
    async def run():
        client, srv = await _client()
        try:
            cross = (await client.post(
                "/cam.webrtc/offer", json={"sdp": "x", "type": "offer"},
                headers={"Origin": "https://evil.example"})).status
            plain = (await client.post(
                "/cam.webrtc/offer", data=b'{"sdp":"x","type":"offer"}',
                headers={"Content-Type": "text/plain"})).status
            rebind = (await client.get(
                "/healthz", headers={"Host": "evil.example"})).status
            bad = (await client.post("/cam.webrtc/offer",
                                     json=["not", "an", "object"])).status
            return cross, plain, rebind, bad
        finally:
            await client.close()
            await srv.stop()
    assert asyncio.run(run()) == (403, 415, 421, 400)


def test_peer_stuck_in_new_is_closed():
    async def run():
        client, srv = await _client(peer_setup_timeout_s=0.3)
        browser = RTCPeerConnection()
        try:
            browser.addTransceiver("video", direction="recvonly")
            await browser.setLocalDescription(await browser.createOffer())
            while browser.iceGatheringState != "complete":
                await asyncio.sleep(0.05)
            r = await client.post("/cam.webrtc/offer", json={
                "sdp": browser.localDescription.sdp,
                "type": browser.localDescription.type})
            assert r.status == 200
            # The "browser" never applies the answer: no connection.
            assert len(srv.peer_connections) == 1
            await asyncio.sleep(1.0)
            return len(srv.peer_connections)
        finally:
            await browser.close()
            await client.close()
            await srv.stop()
    assert asyncio.run(run()) == 0
