"""beambam.handy_key without a phone: finding the app cert and the key's prime in a heap dump, the saved files,
and that every tap waits for Handy to have the focus. (A live run against a rooted Saga recovered the same key,
app cert and cert_id as the working setup, 2026-11-26.)"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

from cryptography import x509                                     # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa         # noqa: E402
from cryptography.x509.oid import NameOID                         # noqa: E402

from beambam import handy_key as hk                               # noqa: E402


def _cert(key: rsa.RSAPrivateKey, issuer_cn: str = "GLOF3813734089.bambulab.com") -> x509.Certificate:
    """A leaf shaped like Handy's: subject GLOF<n>-<hex>, issuer GLOF<n>.bambulab.com (self-signed here)."""
    now = dt.datetime(2026, 7, 23, tzinfo=dt.timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "GLOF3813734089-af70cf3b0000")])
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer_cn)])
    return (x509.CertificateBuilder().subject_name(name).issuer_name(issuer).public_key(key.public_key())
            .serial_number(0xA1B2C3).not_valid_before(now).not_valid_after(now + dt.timedelta(days=550))
            .sign(key, hashes.SHA256()))


@pytest.fixture(scope="module")
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def test_the_app_cert_is_found_as_pem_and_as_der(key):
    cert = _cert(key)
    pem = cert.public_bytes(serialization.Encoding.PEM)
    der = cert.public_bytes(serialization.Encoding.DER)
    other = _cert(rsa.generate_private_key(public_exponent=65537, key_size=2048), issuer_cn="BBL CA")
    junk = b"\x00\x30\x82" * 500
    assert [c.serial_number for c in hk.app_certs(junk + pem + junk)] == [0xA1B2C3]
    assert [c.serial_number for c in hk.app_certs(junk + der + junk)] == [0xA1B2C3]
    # Not a Handy leaf (another issuer), and one cert seen twice is one cert.
    assert list(hk.app_certs(other.public_bytes(serialization.Encoding.DER))) == []
    assert len(list(hk.app_certs(pem + der))) == 1


def test_the_prime_in_the_heap_gives_the_key(key):
    cert = _cert(key)
    nums = key.private_numbers()
    heap = bytearray(np.random.default_rng(7).integers(0, 256, 300_000, dtype=np.uint8).tobytes())
    at = 123_456 - 123_456 % 4                       # Dart keeps BigInt digits 4-byte aligned
    heap[at:at + 128] = nums.q.to_bytes(128, "little")
    p = hk.factor_in(bytes(heap), nums.public_numbers.n)
    assert p in (nums.p, nums.q)
    got = hk.private_key(p, cert)
    # Same primes; the private exponent may differ (phi vs lcm), and both sign for the same public key.
    assert {got.private_numbers().p, got.private_numbers().q} == {nums.p, nums.q}
    from cryptography.hazmat.primitives.asymmetric import padding
    sig = got.sign(b"pause", padding.PKCS1v15(), hashes.SHA256())
    key.public_key().verify(sig, b"pause", padding.PKCS1v15(), hashes.SHA256())
    assert hk.factor_in(bytes(heap[:at]), nums.public_numbers.n) is None


def test_save_keeps_a_different_previous_copy(tmp_path, key):
    cert = _cert(key)
    found = hk.Found(cert, key, "a1b2c3CN=GLOF3813734089.bambulab.com")
    (tmp_path / "printer_cert_id.txt").write_text("oldCN=GLOF1.bambulab.com")
    written = hk.save(found, tmp_path, say=lambda _: None)
    assert {p.name for p in written} == {"printer_sign_key.pem", "printer_app_cert.pem", "printer_cert_id.txt"}
    assert (tmp_path / "printer_sign_key.pem").stat().st_mode & 0o777 == 0o600
    assert len(list(tmp_path.glob("printer_cert_id.txt.bak-*"))) == 1
    hk.save(found, tmp_path, say=lambda _: None)        # the same again: no new backups
    assert len(list(tmp_path.glob("*.bak-*"))) == 1
    wrong = hk.Found(cert, rsa.generate_private_key(public_exponent=65537, key_size=2048), found.cert_id)
    with pytest.raises(hk.KeyError_, match="does not match"):
        hk.save(wrong, tmp_path / "x", say=lambda _: None)


class _FakePhone(hk.Phone):
    """Handy's tab bar on screen; `focus_seq` is what dumpsys reports before each input."""

    def __init__(self, focus_seq: list[str]) -> None:
        super().__init__("fake")
        self.focus_seq, self.inputs = focus_seq, []

    def focus(self) -> str:
        return self.focus_seq.pop(0) if len(self.focus_seq) > 1 else self.focus_seq[0]

    def sh(self, cmd: str, timeout: float | None = None) -> str:
        if cmd.startswith("uiautomator"):
            return ('<node text="" content-desc="Models" bounds="[0,2176][360,2219]"/>'
                    '<node text="" content-desc="Devices" bounds="[360,2176][720,2219]"/>')
        if cmd.startswith("input"):
            self.inputs.append(cmd)
        return ""


def test_the_devices_tab_is_tapped_only_while_handy_has_the_focus():
    handy = f"{hk.HANDY}/com.bambulab.bambulab.MainActivity"
    phone = _FakePhone([handy])
    assert hk._tap_devices_tab(phone, lambda _: None) and phone.inputs == ["input tap 540 2197"]
    other = _FakePhone(["com.google.android.youtube/.Main"])
    assert not hk._tap_devices_tab(other, lambda _: None) and other.inputs == []


def test_heap_regions_skip_java_heaps_files_and_stacks():
    maps = "\n".join([
        "7200053000-7202153000 rw-p 00000000 00:00 0 [anon]",
        "7300000000-7301000000 rw-p 00000000 00:00 0 [anon:dalvik-main space]",
        "7400000000-7401000000 rw-p 00000000 00:00 0 [anon:stack_and_tls:25862]",
        "7500000000-7501000000 rw-p 00000000 fd:00 12 /dev/kgsl-3d0",
        "7600000000-7600010000 rw-p 00000000 00:00 0 [anon:small]",
        "7700000000-7701000000 r--p 00000000 00:00 0 [anon:readonly]"])

    class P(hk.Phone):
        def su(self, cmd: str, timeout: float | None = None) -> bytes:
            return maps.encode()
    assert [r[2] for r in hk.heap_regions(P("x"), "1")] == ["[anon]"]
