# Extract per-install cert + key from a real BambuStudio install

After exhausting every cracking path on-device, this is the **cheapest
viable route** to a working per-installation cert+key the X2D firmware
will accept for `print.*` MQTT commands.

ETA: ~30 minutes. No reverse-engineering required.

## What you need

- Any glibc-x86_64 Linux / macOS / Windows machine that can run the
  official BambuStudio (NOT Termux, NOT Android — those use the same
  VMProtect-wrapped plugin we can't unpack).
- The Bambu account credentials you used to bind your X2D
  (`willstone@gmail.com` for the user this guide targets).
- ~5 minutes of foreground BambuStudio use on that machine.

## Why this works

When BambuStudio cloud-logs-in for the first time on a new install,
its bundled `libbambu_networking.so` plugin:

1. Generates an RSA-2048 keypair **locally** (key never leaves disk)
2. Computes the `x-bbl-device-security-sign` HMAC inside its
   VMProtect-wrapped signing routine using its embedded bootstrap key
3. POSTs the public half + signatures to
   `api.bambulab.com/v1/iot-service/api/user/applications/<app>/cert`
4. Bambu's CA returns the signed cert (the corresponding **public**
   half, AES-256-OAEP-wrapped with a key derived in step 2)
5. Plugin decrypts the response and writes **cert + private key** to
   disk at the OS-specific BambuStudio config dir

After step 5, the cert + key sit on disk in plaintext. **Anyone with
filesystem access can read them.** Including us, on another machine,
after `scp`'ing them over.

The cert is a real Bambu-CA-issued certificate (chain:
`<your-cert> ← GLOF<account>.bambulab.com ← application_root ← BBL CA`).
The X2D firmware's trust list contains BBL CA → it accepts ANY cert
chained to it. The private key is the one BS generated locally — there's
no shared-secret-with-cloud, no hardware binding, no anti-rollback
checks.

## Step-by-step

### 1. Install BambuStudio on a real x86_64 machine

- Linux: `flatpak install com.bambulab.BambuStudio`, OR download the
  AppImage from <https://github.com/bambulab/BambuStudio/releases>.
- macOS: download the .dmg from the same releases page.
- Windows: download the installer.

### 2. Launch + cloud-log-in

Open BambuStudio. Top-left **Login/Register**. Use your Bambu account
credentials. After login, BS should show your bound X2D in the **Device
tab**. (You don't have to actually print anything — just login is
enough.)

If BS shows "Network plug-in not installed" or similar, click
**Settings → Network Plug-in → Download / Install**. BS pulls
`libbambu_networking.so` from Bambu's CDN. Re-login afterwards.

### 3. Verify the cert was installed

After login, the cert+key files appear under the OS-specific BS
config dir:

| OS | Path |
|---|---|
| Linux | `~/.config/BambuStudio/cert/` |
| Linux (Flatpak) | `~/.var/app/com.bambulab.BambuStudio/config/BambuStudio/cert/` |
| macOS | `~/Library/Application Support/BambuStudio/cert/` |
| Windows | `%APPDATA%\BambuStudio\cert\` |

Expected files (names vary slightly by version — match by extension):

```
<account_or_dev_id>.crt         PEM cert (RSA-2048, BBL CA chain)
<account_or_dev_id>.key         PEM private key (RSA-2048)
<account_or_dev_id>.crt.bk      backup of cert (optional)
<account_or_dev_id>.key.bk      backup of key (optional)
```

Confirm cert chain:

```bash
openssl x509 -in <cert>.crt -noout -subject -issuer -dates
# Expect:
# subject= O=GLOF<your-account-id>-<install-hex>, CN=GLOF<...>-<...>
# issuer= CN=GLOF<your-account-id>.bambulab.com
```

### 4. Copy cert + key off the machine

Securely transfer the two files to your Termux device (over LAN, USB,
SD card — whatever's convenient). Suggested destination:

```
~/.x2d/device_cert.crt
~/.x2d/device_cert.key
chmod 600 ~/.x2d/device_cert.*
```

### 5. Wire them into x2d_bridge.py

Edit `bambu_cert.py` in this repo:

```python
# Replace the embedded BAMBU_PRIVATE_KEY_PEM constant with a loader:
def _load_cert_and_key():
    cert_path = Path("~/.x2d/device_cert.crt").expanduser()
    key_path  = Path("~/.x2d/device_cert.key").expanduser()
    if not cert_path.is_file() or not key_path.is_file():
        raise SystemExit(
            f"per-install cert/key not found at {cert_path} / {key_path}. "
            f"See docs/EXTRACT_CERT_FROM_REAL_BS.md for the one-time setup.")
    return cert_path.read_text(), key_path.read_text()

BAMBU_CERT_PEM, BAMBU_PRIVATE_KEY_PEM = _load_cert_and_key()

# Derive cert_id from the cert's CN. Format: GLOF<account>-<hex>
def _derive_cert_id():
    import re, subprocess
    out = subprocess.check_output(
        ["openssl","x509","-in", str(Path("~/.x2d/device_cert.crt").expanduser()),
         "-noout","-subject"]).decode()
    m = re.search(r"CN\s*=\s*(GLOF\w+-\w+)", out)
    if not m:
        raise SystemExit(f"could not parse cert_id from cert subject: {out}")
    return m.group(1)

BAMBU_CERT_ID = _derive_cert_id()
```

Or copy-paste the PEM content directly into `bambu_cert.py`'s
constants if you prefer that style.

### 6. Test

```bash
# First, confirm signing works at all (system.* path always does):
python3.12 x2d_bridge.py chamber-light on

# Then attempt a print — this is the test that proves the cert was
# accepted for print.* commands. If it transitions out of FINISH,
# the wall is broken.
python3.12 x2d_bridge.py print rumi_gold.gcode.3mf \
    --slot 3 --no-upload --timelapse --force
```

Watch `x2d_bridge.py status` — the printer should transition
`FINISH → PREPARE → RUNNING`. That's the success signal. If still
silently dropped, the cert isn't actually in the firmware's trust list
for `print.*` (rare, would suggest the cert was revoked).

## Caveats

- **Cert rotates** every ~18 months (the cert validity is typically 1.5
  years). When yours expires, repeat steps 1-4 on a fresh x86_64 BS
  install or trigger a cert-refresh via Bambu Studio. Our daily
  `bambu_cert.py validate` cron (item #74 in IMPROVEMENTS.md) will
  alert before expiry.
- **One cert per BS install**. If you log in on multiple machines, each
  gets its own cert+key pair — they're independent and all valid.
- **Don't share the key**. It's tied to your Bambu account. Sharing
  lets someone else publish to your printer's MQTT.
- **If you revoke the install** (Bambu account → Devices → Remove
  Bind), the cert is revoked → firmware rejects it → repeat steps 1-4
  on a fresh install.
- The MakerWorld browser session does NOT have a usable cert+key —
  browsers use HttpOnly-cookie auth, not the MQTT signing scheme.
  Same for the Bambu Handy Android app: its cert+key are stored at
  `/data/data/bbl.intl.bambulab.com/` which is root-only.

## Why we can't extract on-device

Documented exhaustively in `runtime/bambu_extract/README.md`:

1. **Static RE of the Linux x86_64 .so**: packed VMProtect; ~3.3 MB of
   encrypted code; weeks of dedicated RE to unpack.
2. **qemu-x86_64 dlopen of the unpacked Mac dylib**: confirmed works
   for dlopen + symbol resolution + all init calls, but `connect_server`
   fails -2 because the cloud-broker MQTT-auth handshake requires the
   bootstrap client cert that lives encrypted in `__TEXT,__const` and
   is only decrypted inside the same VMProtect VM. Different attack
   surface, same wall.
3. **Frida-hook a running Bambu Handy process** (pending task #24):
   Android `seccomp` filter blocks `ptrace` attach (Layer-4) on
   non-debuggable apps; Frida's gadget-injection mode would require
   repacking the Bambu APK + sideloading, which user hasn't authorized.
4. **adb backup / run-as**: Bambu Handy is release-signed
   (`allowBackup` not set; not debuggable). All paths to its
   `/data/data/` dir are root-blocked. The user's phone isn't rooted.
5. **/sdcard scan**: searched. Only contains cached model 3MFs +
   thumbnail cache + Agora SDK logs. No cert material.

## Status of files in this round

Committed in this session (date branch — not pushed):
- `runtime/bambu_extract/` — qemu-x86_64 wrap scaffolding + LD_PRELOAD
  trace shim with anti-debug spoofing. Confirms the VMProtect wall.
- `docs/EXTRACT_CERT_FROM_REAL_BS.md` — this doc.

Pending in `IMPROVEMENTS.md`:
- #24 Frida-extract from Bambu Handy (blocked Layer-4 seccomp)
- #16 Pattern-scan libapp.so for bundled BoringSSL (no longer
  immediately useful — boringssl in libapp is for Flutter HTTP cert
  pinning, not the MQTT signing path)
