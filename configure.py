#!/usr/bin/env python3
"""Interactive configuration for the macOS standalone gateway."""

import getpass
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def prompt(label, default="", secret=False):
    suffix = " [{}]".format(default) if default else ""
    value = (getpass.getpass if secret else input)("{}{}: ".format(label, suffix)).strip()
    return value or default


def local_address():
    for interface in ("en0", "en1", "en2"):
        try:
            result = subprocess.run(["ipconfig", "getifaddr", interface], capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        result = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().split()[0]
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def clean(value):
    return value.replace("\r", "").replace("\n", "")


print("Kayo + Binge standalone gateway setup\n")
print("Use one shared login, or leave it blank to enter separate accounts.")
shared_email = prompt("Shared Kayo/Binge email (optional)")
values = {}
if shared_email:
    values["TV_EMAIL"] = shared_email
    values["TV_PASSWORD"] = prompt("Shared password", secret=True)
else:
    values["BINGE_EMAIL"] = prompt("Binge email")
    values["BINGE_PASSWORD"] = prompt("Binge password", secret=True)
    values["KAYO_EMAIL"] = prompt("Kayo email")
    values["KAYO_PASSWORD"] = prompt("Kayo password", secret=True)

address = prompt("Mac's local network IP", local_address() or "YOUR_MAC_IP")
port = prompt("Port", "8787")
values.update({"HOST": "0.0.0.0", "PORT": port, "PUBLIC_BASE_URL": "http://{}:{}".format(address, port)})

lines = ["{}={}".format(key, clean(value)) for key, value in values.items()]
(ROOT / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
(ROOT / ".env").chmod(0o600)
print("\nSaved {} with owner-only permissions.".format(ROOT / ".env"))
