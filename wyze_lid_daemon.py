#!/usr/bin/env python3
"""
Daemon para encender y apagar automáticamente múltiples Wyze Switches / Wyze Plugs
('Laptop' y 'Fan') al abrir y cerrar la pantalla (tapa) de una MacBook o al
encenderla/apagarla remotamente con la tapa cerrada, con descarga automática
hasta 80% para el enchufe 'Laptop'.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from wyze_sdk import Client
from wyze_sdk.errors import WyzeApiError, WyzeRequestError

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
CACHE_PATH = BASE_DIR / ".cache.json"
LOG_PATH = BASE_DIR / "wyze_lid.log"
REMOTE_AWAKE_FLAG = BASE_DIR / ".remote_awake"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("wyze_lid")


def load_cache() -> Dict[str, Any]:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_cache(data: Dict[str, Any]) -> None:
    try:
        CACHE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("No se pudo guardar cache: %s", exc)


def get_client(force_login: bool = False) -> Client:
    load_dotenv(ENV_PATH, override=True)
    email = os.getenv("WYZE_EMAIL", "").strip()
    password = os.getenv("WYZE_PASSWORD", "").strip()
    key_id = os.getenv("WYZE_KEY_ID", "").strip()
    api_key = os.getenv("WYZE_API_KEY", "").strip()
    totp_key = os.getenv("WYZE_TOTP_KEY", "").strip() or None

    if not email or not password or not key_id or not api_key:
        raise RuntimeError(
            "Faltan credenciales en .env (WYZE_EMAIL, WYZE_PASSWORD, WYZE_KEY_ID, WYZE_API_KEY)."
        )

    cache = load_cache()
    if not force_login and cache.get("access_token"):
        return Client(
            token=cache["access_token"],
            refresh_token=cache.get("refresh_token"),
            timeout=10,
        )

    logger.info("Iniciando sesión en Wyze API...")
    client = Client(
        email=email,
        password=password,
        key_id=key_id,
        api_key=api_key,
        totp_key=totp_key,
        timeout=15,
    )
    cache["access_token"] = client._token
    cache["refresh_token"] = client._refresh_token
    save_cache(cache)
    return client


def discover_devices(client: Client) -> List[Dict[str, str]]:
    load_dotenv(ENV_PATH, override=True)
    raw_names = os.getenv("WYZE_DEVICE_NAMES", os.getenv("WYZE_DEVICE_NAME", "")).strip()
    target_names = [n.strip().lower() for n in raw_names.split(",") if n.strip()]

    candidates = []
    for plug in client.plugs.list():
        candidates.append(
            {
                "type": "plug",
                "mac": plug.mac,
                "model": plug.product.model,
                "nickname": (plug.nickname or "").strip(),
            }
        )
    for switch in client.switches.list():
        candidates.append(
            {
                "type": "switch",
                "mac": switch.mac,
                "model": switch.product.model,
                "nickname": (switch.nickname or "").strip(),
            }
        )

    if not candidates:
        raise RuntimeError("No se encontraron enchufes (Plugs) ni Switches en tu cuenta Wyze.")

    selected: List[Dict[str, str]] = []
    if target_names:
        for tname in target_names:
            matched = None
            for dev in candidates:
                if dev["nickname"].lower() == tname or tname in dev["nickname"].lower():
                    matched = dev
                    break
            if matched:
                selected.append(matched)
            else:
                names = ", ".join(f"'{c['nickname']}' ({c['type']})" for c in candidates)
                logger.warning(
                    "No se encontró dispositivo '%s'. Disponibles: %s", tname, names
                )
        if not selected:
            names = ", ".join(f"'{c['nickname']}' ({c['type']})" for c in candidates)
            raise RuntimeError(
                f"No se encontró ninguno de los dispositivos {target_names}. Disponibles: {names}"
            )
    else:
        selected = candidates

    cache = load_cache()
    cache["devices"] = selected
    save_cache(cache)
    for dev in selected:
        logger.info(
            "Dispositivo vinculado: '%s' (%s | MAC: %s | Modelo: %s)",
            dev["nickname"],
            dev["type"],
            dev["mac"],
            dev["model"],
        )
    return selected


def _toggle_single_device(client: Client, dev: Dict[str, str], turn_on: bool) -> None:
    dev_type = dev["type"]
    dev_mac = dev["mac"]
    dev_model = dev["model"]
    if dev_type == "switch":
        if turn_on:
            client.switches.turn_on(device_mac=dev_mac, device_model=dev_model)
        else:
            client.switches.turn_off(device_mac=dev_mac, device_model=dev_model)
    else:
        if turn_on:
            client.plugs.turn_on(device_mac=dev_mac, device_model=dev_model)
        else:
            client.plugs.turn_off(device_mac=dev_mac, device_model=dev_model)


def set_devices_states(desired_states: Dict[str, bool], max_retries: int = 3) -> bool:
    """
    desired_states: dict de {nickname_lower: True/False} o {'*': True/False}
    """
    for attempt in range(1, max_retries + 1):
        try:
            cache = load_cache()
            client = get_client(force_login=(attempt > 1))
            devices = cache.get("devices")
            if not devices:
                devices = discover_devices(client)

            tasks = []
            for dev in devices:
                nick_lower = dev.get("nickname", "").lower()
                if nick_lower in desired_states:
                    target_on = desired_states[nick_lower]
                elif "*" in desired_states:
                    target_on = desired_states["*"]
                else:
                    continue
                tasks.append((dev, target_on))

            if not tasks:
                return True

            with ThreadPoolExecutor(max_workers=max(1, len(tasks))) as pool:
                futures = [
                    pool.submit(_toggle_single_device, client, dev, target_on)
                    for dev, target_on in tasks
                ]
                for fut in futures:
                    fut.result()

            summary = ", ".join(
                f"'{dev.get('nickname', dev['mac'])}'={'ON' if t_on else 'OFF'}"
                for dev, t_on in tasks
            )
            logger.info("Estado Wyze actualizado: [%s] (intento %d)", summary, attempt)
            return True
        except (WyzeApiError, WyzeRequestError, Exception) as exc:
            logger.warning(
                "Intento %d/%d falló al actualizar Wyze: %s", attempt, max_retries, exc
            )
            if attempt < max_retries:
                time.sleep(1.5)
    return False


def set_wyze_power(turn_on: bool, max_retries: int = 3) -> bool:
    return set_devices_states({"*": turn_on}, max_retries=max_retries)


def is_lid_closed() -> Optional[bool]:
    """Devuelve True si la tapa de la Mac está cerrada, False si está abierta."""
    try:
        out = subprocess.check_output(
            ["/usr/sbin/ioreg", "-r", "-k", "AppleClamshellState", "-d", "4"],
            text=True,
            timeout=2,
        )
        for line in out.splitlines():
            if "AppleClamshellState" in line:
                return "= Yes" in line
    except Exception as exc:
        logger.debug("Error leyendo AppleClamshellState: %s", exc)
    return None


def get_power_info() -> tuple[Optional[int], Optional[bool]]:
    """Devuelve (porcentaje_bateria, ac_conectado)."""
    try:
        out = subprocess.check_output(["/usr/bin/pmset", "-g", "batt"], text=True, timeout=2)
        match = re.search(r"(\d+)%", out)
        pct = int(match.group(1)) if match else None
        ac = "AC Power" in out or "AC attached" in out
        return pct, ac
    except Exception as exc:
        logger.debug("Error leyendo pmset batt: %s", exc)
    return None, None


def get_battery_percentage() -> Optional[int]:
    pct, _ = get_power_info()
    return pct


def is_remote_awake() -> bool:
    return REMOTE_AWAKE_FLAG.exists()


def set_remote_awake(enabled: bool) -> None:
    try:
        if enabled:
            REMOTE_AWAKE_FLAG.write_text(str(int(time.time())), encoding="utf-8")
            subprocess.run(["sudo", "-n", "/usr/bin/pmset", "-a", "disablesleep", "1"], check=False)
        else:
            if REMOTE_AWAKE_FLAG.exists():
                REMOTE_AWAKE_FLAG.unlink()
            subprocess.run(["sudo", "-n", "/usr/bin/pmset", "-a", "disablesleep", "0"], check=False)
    except Exception as exc:
        logger.warning("Error cambiando estado remote_awake (%s): %s", enabled, exc)


def list_devices() -> None:
    client = get_client(force_login=True)
    discover_devices(client)
    plugs = client.plugs.list()
    switches = client.switches.list()
    print("\n=== Dispositivos Wyze en tu cuenta ===")
    for p in plugs:
        print(f"- [Plug]   Nombre: '{p.nickname}' | MAC: {p.mac} | Modelo: {p.product.model} | Encendido: {p.is_on}")
    for s in switches:
        print(f"- [Switch] Nombre: '{s.nickname}' | MAC: {s.mac} | Modelo: {s.product.model}")
    if not plugs and not switches:
        print("(Ningún Plug o Switch encontrado en esta cuenta)")
    print("======================================\n")


def compute_open_lid_states(battery_pct: Optional[int], max_limit: int = 80) -> Dict[str, bool]:
    """
    Cuando la Mac está ACTIVA (tapa abierta o encendida remotamente):
    - 'fan' (y cualquier otro): siempre ON.
    - 'laptop':
        * Si la batería está por encima de max_limit (ej. > 80%), OFF para que descargue hasta 80%.
        * Si la batería está en <= max_limit (ej. <= 80%), ON (y AlDente la mantiene congelada en 80%).
    """
    laptop_on = True
    if battery_pct is not None and battery_pct > max_limit:
        laptop_on = False
    return {
        "*": True,
        "fan": True,
        "laptop": laptop_on,
    }


def remote_wake_action() -> None:
    load_dotenv(ENV_PATH, override=True)
    max_limit = int(os.getenv("MAX_BATTERY_LIMIT", "80"))
    set_remote_awake(True)
    batt = get_battery_percentage()
    desired = compute_open_lid_states(batt, max_limit)
    logger.info(
        "🌐 ENCENDIDO REMOTO activado (Batería: %s%%) -> Encendiendo 'Laptop'=%s y 'Fan'=ON",
        batt,
        "ON" if desired["laptop"] else f"OFF (descargando a {max_limit}%)",
    )
    set_devices_states(desired, max_retries=5)


def remote_sleep_action() -> None:
    logger.info("💤 SUSPENSIÓN REMOTA solicitada -> Apagando 'Laptop' y 'Fan' y restaurando reposo...")
    set_devices_states({"*": False, "laptop": False, "fan": False}, max_retries=4)
    set_remote_awake(False)


def listen_remote_ntfy() -> None:
    """Escucha comandos remotos ('dormir' / 'despertar') por Internet/5G vía ntfy.sh."""
    url = "https://ntfy.sh/raydel-mac-774921/json"
    while True:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "MacRemoteControl/1.0"})
            with urllib.request.urlopen(req, timeout=90) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="ignore").strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    if data.get("event") != "message":
                        continue
                    msg = (data.get("message") or "").strip().lower()
                    if msg in ("dormir", "off", "apagar", "sleep"):
                        logger.info("📱 Botón remoto 5G/Internet recibido ('%s') -> Ejecutando dormir.sh", msg)
                        subprocess.Popen(["/Users/raydelsantana/dormir.sh"])
                    elif msg in ("despertar", "on", "encender", "wake"):
                        logger.info("📱 Botón remoto 5G/Internet recibido ('%s') -> Ejecutando despertar.sh", msg)
                        subprocess.Popen(["/Users/raydelsantana/despertar.sh"])
        except Exception as exc:
            logger.debug("Reintentando conexión ntfy: %s", exc)
            time.sleep(3.0)


def run_daemon() -> None:
    load_dotenv(ENV_PATH, override=True)
    max_limit = int(os.getenv("MAX_BATTERY_LIMIT", "80"))
    logger.info(
        "Iniciando monitor de tapa + modo remoto + límite de batería (%d%%) para Wyze...", max_limit
    )
    threading.Thread(target=listen_remote_ntfy, daemon=True).start()
    try:
        client = get_client()
        discover_devices(client)
    except Exception as exc:
        logger.warning("Aviso inicial al pre-cargar dispositivos: %s", exc)

    last_closed = is_lid_closed()
    last_remote = is_remote_awake()
    batt, last_ac = get_power_info()
    last_active = (last_closed is False) or last_remote
    lid_closed_off_timestamp = time.time() if not last_active else 0.0
    daemon_laptop_turnoff_ts = 0.0

    logger.info(
        "Estado inicial -> Tapa: %s | Remoto: %s | Batería: %s%% | AC: %s",
        "CERRADA" if last_closed else "ABIERTA",
        "ACTIVO" if last_remote else "INACTIVO",
        batt if batt is not None else "?",
        "SÍ" if last_ac else "NO",
    )

    last_applied_states: Optional[Dict[str, bool]] = None
    last_batt_check = 0.0

    if last_active:
        desired = compute_open_lid_states(batt, max_limit)
        if not desired["laptop"]:
            daemon_laptop_turnoff_ts = time.time()
        if set_devices_states(desired, max_retries=4):
            last_applied_states = desired
            _, last_ac = get_power_info()
        last_batt_check = time.time()

    while True:
        closed = is_lid_closed()
        remote = is_remote_awake()
        batt, ac_now = get_power_info()
        now = time.time()

        # Si el usuario abre físicamente la tapa, salimos de modo remoto y pasamos a modo tapa abierta normal
        if closed is False and last_closed is True and remote:
            logger.info("Tapa abierta físicamente -> Desactivando bandera de modo remoto (continúa activa por tapa abierta)")
            set_remote_awake(False)
            remote = False

        # Si el usuario cierra físicamente la tapa (estando antes abierta), apagamos todo y limpiamos modo remoto
        if closed is True and last_closed is False:
            if remote:
                set_remote_awake(False)
                remote = False

        # 1. Detección de ENCENDIDO por enchufe 'Laptop' (AC Wake con tapa cerrada):
        # Si la tapa lleva cerrada > 6s en estado inactivo y hay corriente AC
        # (porque el usuario encendió el enchufe 'Laptop' desde el celular o el botón remoto),
        # activamos modo remoto automáticamente sin depender de last_ac previo al reposo!
        if (
            closed is True
            and not remote
            and ac_now is True
            and (now - lid_closed_off_timestamp) > 6.0
        ):
            logger.info(
                "⚡ Detectada entrada de energía AC con tapa cerrada (enchufe 'Laptop' encendido remotamente) -> Activando Modo Remoto y encendiendo 'Fan'!"
            )
            set_remote_awake(True)
            remote = True
            subprocess.Popen(["/usr/bin/caffeinate", "-u", "-t", "10"])

        # 2. Detección de APAGADO por enchufe 'Laptop' con tapa cerrada:
        # Si estamos en Modo Remoto con la tapa cerrada, el demonio quería tener 'Laptop' en ON,
        # y de pronto se corta la corriente AC (porque el usuario apagó el enchufe 'Laptop' desde Wyze):
        elif (
            closed is True
            and remote
            and ac_now is False
            and last_ac is True
            and (last_applied_states is None or last_applied_states.get("laptop", True) is True)
            and (now - daemon_laptop_turnoff_ts) > 15.0
        ):
            time.sleep(2.0)
            _, ac_confirm = get_power_info()
            if ac_confirm is False and is_lid_closed() is True:
                logger.info(
                    "🔌 Detectado apagado manual del enchufe 'Laptop' con tapa cerrada -> Apagando 'Fan', desactivando Modo Remoto y suspendiendo la Mac..."
                )
                subprocess.run(
                    ["/bin/launchctl", "unload", "/Users/raydelsantana/Library/LaunchAgents/com.raydelsantana.caffeinate-dims.plist"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                subprocess.run(["/usr/bin/killall", "caffeinate"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                desired_off = {"*": False, "laptop": False, "fan": False}
                set_devices_states(desired_off, max_retries=3)
                last_applied_states = desired_off
                set_remote_awake(False)
                remote = False
                last_active = False
                lid_closed_off_timestamp = time.time()
                subprocess.Popen(["sudo", "-n", "/usr/bin/pmset", "sleepnow"])
                last_closed = closed
                last_remote = remote
                last_ac = ac_confirm
                time.sleep(2.0)
                continue

        active = (closed is False) or remote

        if closed is not None and active != last_active:
            if not active:
                logger.info("Detectado: INACTIVA (Tapa cerrada / Dormir) -> Apagando 'Laptop' y 'Fan' inmediatamente...")
                subprocess.run(
                    ["/bin/launchctl", "unload", "/Users/raydelsantana/Library/LaunchAgents/com.raydelsantana.caffeinate-dims.plist"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                subprocess.run(["/usr/bin/killall", "caffeinate"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                desired = {"*": False, "laptop": False, "fan": False}
                if set_devices_states(desired, max_retries=3):
                    last_applied_states = desired
                lid_closed_off_timestamp = time.time()
            else:
                subprocess.run(
                    ["/bin/launchctl", "load", "-w", "/Users/raydelsantana/Library/LaunchAgents/com.raydelsantana.caffeinate-dims.plist"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                desired = compute_open_lid_states(batt, max_limit)
                if not desired["laptop"]:
                    daemon_laptop_turnoff_ts = now
                modo_str = "MODO REMOTO (Tapa cerrada)" if closed else "TAPA ABIERTA"
                logger.info(
                    "Detectado: %s (Batería: %s%%) -> Aplicando estado: Laptop=%s, Fan=ON",
                    modo_str,
                    batt,
                    "ON" if desired["laptop"] else f"OFF (descargando a {max_limit}%)",
                )
                if set_devices_states(desired, max_retries=10):
                    last_applied_states = desired
                last_batt_check = now
            last_active = active

        # Mientras esté activa (tapa abierta o modo remoto), revisar cada 30s el límite de 80% de batería
        elif active and (now - last_batt_check) >= 30.0:
            last_batt_check = now
            desired = compute_open_lid_states(batt, max_limit)
            if last_applied_states is None or desired.get("laptop") != last_applied_states.get("laptop"):
                if desired["laptop"]:
                    logger.info(
                        "Batería alcanzó %s%% (<= %d%%) -> Encendiendo 'Laptop' para mantener en %d%%",
                        batt,
                        max_limit,
                        max_limit,
                    )
                else:
                    daemon_laptop_turnoff_ts = now
                    logger.info(
                        "Batería en %s%% (> %d%%) -> Apagando 'Laptop' para descargar hasta %d%%",
                        batt,
                        max_limit,
                        max_limit,
                    )
                if set_devices_states({"laptop": desired["laptop"]}, max_retries=4):
                    last_applied_states = desired

        last_closed = closed
        last_remote = remote
        last_ac = ac_now
        time.sleep(0.25)


def main() -> None:
    parser = argparse.ArgumentParser(description="Control automático de Wyze según la tapa, modo remoto y batería de la Mac")
    parser.add_argument("--list", action="store_true", help="Listar todos los Plugs y Switches de la cuenta")
    parser.add_argument("--on", action="store_true", help="Encender los dispositivos Wyze ahora")
    parser.add_argument("--off", action="store_true", help="Apagar los dispositivos Wyze ahora")
    parser.add_argument("--remote-wake", action="store_true", help="Activar modo remoto con tapa cerrada y encender Laptop y Fan")
    parser.add_argument("--remote-sleep", action="store_true", help="Apagar Laptop y Fan y desactivar modo remoto")
    parser.add_argument("--daemon", action="store_true", help="Ejecutar el monitor continuo de tapa y batería")
    args = parser.parse_args()

    if args.list:
        list_devices()
    elif args.remote_wake:
        remote_wake_action()
    elif args.remote_sleep:
        remote_sleep_action()
    elif args.on:
        set_wyze_power(turn_on=True)
    elif args.off:
        set_wyze_power(turn_on=False)
    else:
        run_daemon()


if __name__ == "__main__":
    main()
