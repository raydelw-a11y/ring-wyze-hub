#!/usr/bin/env python3
"""
Herramienta local en tu Mac para ver y autorizar/revocar dispositivos (ej. Tablet)
que intentan acceder a tu panel Ring + Wyze en Render.

Uso:
  ./autorizar.py                -> Lista dispositivos pendientes y autorizados
  ./autorizar.py 4829           -> Autoriza el dispositivo con código 4829
  ./autorizar.py --all          -> Autoriza todos los dispositivos pendientes
  ./autorizar.py --revoke 4829  -> Revoca el acceso al código 4829
"""

import json
import os
import sys
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"


def load_env() -> dict:
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def main() -> None:
    env = load_env()
    server_url = os.getenv("HUB_URL", env.get("HUB_URL", "http://localhost:8080")).rstrip("/")
    admin_key = os.getenv(
        "MAC_ADMIN_KEY", env.get("MAC_ADMIN_KEY", "raydel-mac-secret-key-2026")
    )

    args = sys.argv[1:]
    if not args:
        req = urllib.request.Request(
            f"{server_url}/api/admin/devices",
            headers={"X-Admin-Key": admin_key},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            print(f"Error conectando a {server_url}: {exc}")
            sys.exit(1)

        devices = data.get("devices", [])
        print(f"\n=== Dispositivos registrados en {server_url} ===")
        if not devices:
            print("  (Ningún dispositivo ha solicitado acceso todavía)")
        for d in devices:
            status_icon = "✅ AUTORIZADO" if d["status"] == "approved" else (
                "⏳ PENDIENTE" if d["status"] == "pending" else "❌ REVOCADO"
            )
            print(
                f"  [{status_icon}] Código: {d['pairing_code']} | Token: {d['device_token'][:12]}... | Creado: {d.get('created_at', '')}"
            )
        print("====================================================\n")
        print("Para aprobar un dispositivo ejecuta:  ./autorizar.py <CODIGO>")
        return

    action = "approve"
    target = args[0]
    if args[0] == "--revoke" and len(args) > 1:
        action = "revoke"
        target = args[1]

    payload = json.dumps({"target": target, "action": action}).encode("utf-8")
    req = urllib.request.Request(
        f"{server_url}/api/admin/devices",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Admin-Key": admin_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"Error enviando orden a {server_url}: {exc}")
        sys.exit(1)

    updated = data.get("updated", [])
    if not updated:
        print(f"⚠️ No se encontró ningún dispositivo con código '{target}'.")
    else:
        for d in updated:
            print(
                f"✅ Dispositivo código {d['pairing_code']} -> Estado cambiado a: {d['status'].upper()}"
            )
            if d["status"] == "approved":
                print(
                    f"   (Token permanente para PREAPPROVED_DEVICE_TOKENS en Render si deseas fijarlo: {d['device_token']})"
                )


if __name__ == "__main__":
    main()
