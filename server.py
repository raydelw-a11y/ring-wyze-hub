#!/usr/bin/env python3
"""
Servidor Web + Automatizador 24/7 Ring Cameras -> Wyze Front Light Plug.
- Muestra las 4 cámaras Ring en una interfaz para Tablet 24/7.
- SOLO actualiza el snapshot específico de la cámara que detecte una PERSONA (human),
  manteniendo las demás cámaras 100% estáticas con su último snapshot.
- Ignora por completo movimientos que no sean personas (vehículos, ramas, etc.).
- Enciende 'Front Light Plug' por 1 minuto (reiniciable) del anochecer al amanecer
  cuando 'Front Door' o 'Driveway Cam' detectan una persona.
- Requiere autorización manual de cada dispositivo desde la Mac (./autorizar.py).
"""

import asyncio
from datetime import datetime, date
import json
import logging
import math
import os
import random
import secrets
import shutil
import sys
import time
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from zoneinfo import ZoneInfo

from aiohttp import web
from dotenv import load_dotenv
from ring_doorbell import Auth, Ring, RingEvent, RingEventListener
from ring_doorbell.const import (
    SETTINGS_ENDPOINT,
    SNAPSHOT_ENDPOINT,
    SNAPSHOT_TIMESTAMP_ENDPOINT,
)
from wyze_sdk import Client as WyzeClient

warnings.filterwarnings("ignore", category=DeprecationWarning)

BASE_DIR = Path(__file__).resolve().parent
DIST_DIR = BASE_DIR / "dist"
ENV_PATH = BASE_DIR / ".env"
WYZE_CACHE_PATH = BASE_DIR / ".cache.json"
RING_TOKEN_PATH = BASE_DIR / ".ring_token.json"
RING_FCM_PATH = BASE_DIR / ".ring_fcm_creds.json"
DEVICES_DB_PATH = BASE_DIR / ".authorized_devices.json"

USER_AGENT = "RaydelRingWyzeBridge/1.0"
LOCAL_TZ = ZoneInfo("America/New_York")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ring_wyze_hub")


# ---------------------------------------------------------------------------
# Cálculo astronómico NOAA de Amanecer (Sunrise) y Anochecer (Sunset)
# ---------------------------------------------------------------------------
def _calc_sun_time_utc(d: date, lat: float, lon: float, is_sunrise: bool) -> Optional[float]:
    day_of_year = d.timetuple().tm_yday
    lng_hour = lon / 15.0
    t = day_of_year + ((6.0 - lng_hour) / 24.0 if is_sunrise else (18.0 - lng_hour) / 24.0)
    m = (0.9856 * t) - 3.289
    l_sun = (
        m
        + (1.916 * math.sin(math.radians(m)))
        + (0.020 * math.sin(math.radians(2 * m)))
        + 282.634
    ) % 360.0
    ra = math.degrees(math.atan(0.91764 * math.tan(math.radians(l_sun)))) % 360.0
    l_quadrant = (math.floor(l_sun / 90.0)) * 90.0
    ra_quadrant = (math.floor(ra / 90.0)) * 90.0
    ra = (ra + (l_quadrant - ra_quadrant)) / 15.0
    sin_dec = 0.39782 * math.sin(math.radians(l_sun))
    cos_dec = math.cos(math.asin(sin_dec))
    cos_h = (
        math.cos(math.radians(90.833)) - (sin_dec * math.sin(math.radians(lat)))
    ) / (cos_dec * math.cos(math.radians(lat)))
    if cos_h > 1 or cos_h < -1:
        return None
    h = (360.0 - math.degrees(math.acos(cos_h))) if is_sunrise else math.degrees(math.acos(cos_h))
    h = h / 15.0
    t_local = h + ra - (0.06571 * t) - 6.622
    ut = (t_local - lng_hour) % 24.0
    return ut * 60.0


def get_sunrise_sunset_local(lat: float, lon: float) -> Dict[str, datetime]:
    now_local = datetime.now(LOCAL_TZ)
    today = now_local.date()
    utc_offset_minutes = now_local.utcoffset().total_seconds() / 60.0

    sr_utc_min = _calc_sun_time_utc(today, lat, lon, is_sunrise=True)
    ss_utc_min = _calc_sun_time_utc(today, lat, lon, is_sunrise=False)

    sr_local_min = (sr_utc_min + utc_offset_minutes) % 1440
    ss_local_min = (ss_utc_min + utc_offset_minutes) % 1440

    sunrise = now_local.replace(
        hour=int(sr_local_min // 60),
        minute=int(sr_local_min % 60),
        second=0,
        microsecond=0,
    )
    sunset = now_local.replace(
        hour=int(ss_local_min // 60),
        minute=int(ss_local_min % 60),
        second=0,
        microsecond=0,
    )
    return {"sunrise": sunrise, "sunset": sunset, "now": now_local}


def is_nighttime(lat: float, lon: float) -> bool:
    times = get_sunrise_sunset_local(lat, lon)
    now = times["now"]
    return now >= times["sunset"] or now <= times["sunrise"]


# ---------------------------------------------------------------------------
# Persistencia de Tokens Ring / Wyze / Dispositivos Autorizados
# ---------------------------------------------------------------------------
def load_json_file(path: Path) -> Dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_json_file(path: Path, data: Dict[str, Any]) -> None:
    try:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("No se pudo guardar %s: %s", path.name, exc)


def save_ring_token(token: Dict[str, Any]) -> None:
    save_json_file(RING_TOKEN_PATH, token)


def load_ring_token() -> Optional[Dict[str, Any]]:
    data = load_json_file(RING_TOKEN_PATH)
    if data:
        return data
    env_tok = os.getenv("RING_INIT_TOKEN_JSON", "").strip()
    if env_tok:
        try:
            parsed = json.loads(env_tok)
            save_ring_token(parsed)
            return parsed
        except Exception:
            pass
    return None


def save_fcm_creds(creds: Dict[str, Any]) -> None:
    save_json_file(RING_FCM_PATH, creds)


def load_fcm_creds() -> Optional[Dict[str, Any]]:
    data = load_json_file(RING_FCM_PATH)
    if data:
        return data
    env_fcm = os.getenv("RING_INIT_FCM_CREDS_JSON", "").strip()
    if env_fcm:
        try:
            parsed = json.loads(env_fcm)
            save_fcm_creds(parsed)
            return parsed
        except Exception:
            pass
    return None


def set_front_light_plug(turn_on: bool) -> bool:
    load_dotenv(ENV_PATH, override=False)
    plug_mac = os.getenv("WYZE_LIGHT_PLUG_MAC", "80482CA69593").strip()
    plug_model = os.getenv("WYZE_LIGHT_PLUG_MODEL", "WLPP1CFH").strip()
    plug_name = os.getenv("WYZE_LIGHT_PLUG_NAME", "Front Light Plug").strip()

    for attempt in (1, 2):
        try:
            cache = load_json_file(WYZE_CACHE_PATH)
            if attempt == 1 and cache.get("access_token"):
                client = WyzeClient(
                    token=cache["access_token"],
                    refresh_token=cache.get("refresh_token"),
                    timeout=10,
                )
            else:
                client = WyzeClient(
                    email=os.getenv("WYZE_EMAIL", "").strip(),
                    password=os.getenv("WYZE_PASSWORD", "").strip(),
                    key_id=os.getenv("WYZE_KEY_ID", "").strip(),
                    api_key=os.getenv("WYZE_API_KEY", "").strip(),
                    timeout=15,
                )
                cache["access_token"] = client._token
                cache["refresh_token"] = client._refresh_token
                save_json_file(WYZE_CACHE_PATH, cache)

            if turn_on:
                client.plugs.turn_on(device_mac=plug_mac, device_model=plug_model)
            else:
                client.plugs.turn_off(device_mac=plug_mac, device_model=plug_model)

            logger.info(
                "💡 Wyze '%s' (%s) -> %s",
                plug_name,
                plug_mac,
                "ENCENDIDO (ON)" if turn_on else "APAGADO (OFF)",
            )
            return True
        except Exception as exc:
            logger.warning("Intento %d falló al cambiar '%s': %s", attempt, plug_name, exc)
    return False


def is_any_human_entry(entry: Dict[str, Any]) -> bool:
    cv = entry.get("cv_properties") or {}
    det_type = (cv.get("detection_type") or "").strip().lower()
    if det_type in ("human", "person") or cv.get("person_detected") is True:
        return True
    for dt in cv.get("detection_types") or []:
        if (dt.get("detection_type") or "").strip().lower() in ("human", "person"):
            return True
    return False


def format_entry_local_datetime(entry: Dict[str, Any]) -> Optional[str]:
    created_raw = entry.get("created_at")
    if not created_raw:
        return None
    try:
        if isinstance(created_raw, str):
            dt = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
        else:
            dt = created_raw
        return dt.astimezone(LOCAL_TZ).strftime("%I:%M:%S %p - %m/%d/%Y")
    except Exception:
        return None


def get_ffmpeg_binary() -> str:
    sys_ff = shutil.which("ffmpeg")
    if sys_ff:
        return sys_ff
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def is_human_history_entry(entry: Dict[str, Any], max_age_seconds: float = 90.0) -> bool:
    created_raw = entry.get("created_at")
    if created_raw:
        try:
            if isinstance(created_raw, str):
                dt = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            else:
                dt = created_raw
            age = (datetime.now(dt.tzinfo) - dt).total_seconds()
            if age > max_age_seconds:
                return False
        except Exception:
            pass

    cv = entry.get("cv_properties") or {}
    det_type = (cv.get("detection_type") or "").strip().lower()
    if det_type in ("human", "person") or cv.get("person_detected") is True:
        return True
    for dt in cv.get("detection_types") or []:
        if (dt.get("detection_type") or "").strip().lower() in ("human", "person"):
            return True
    return False


def extract_human_signatures(entry: Dict[str, Any]) -> List[str]:
    eid = entry.get("id")
    sigs = [f"id:{eid}"]
    cv = entry.get("cv_properties") or {}
    for dt in cv.get("detection_types") or []:
        if (dt.get("detection_type") or "").strip().lower() in ("human", "person"):
            for ts in dt.get("verified_timestamps") or []:
                sigs.append(f"ts:{eid}:{ts}")
    return sigs


# ---------------------------------------------------------------------------
# SVG Placeholder en caso de que una cámara aún esté cargando su primer snapshot
# ---------------------------------------------------------------------------
def make_placeholder_svg(cam_name: str) -> bytes:
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360" viewBox="0 0 640 360">
      <rect width="640" height="360" fill="#0b0f17"/>
      <text x="50%" y="48%" dominant-baseline="middle" text-anchor="middle" fill="#64748b" font-family="sans-serif" font-size="20" font-weight="bold">{cam_name}</text>
      <text x="50%" y="58%" dominant-baseline="middle" text-anchor="middle" fill="#475569" font-family="sans-serif" font-size="13">Cargando snapshot inicial...</text>
    </svg>"""
    return svg.encode("utf-8")


# ---------------------------------------------------------------------------
# Hub Principal Ring + Wyze
# ---------------------------------------------------------------------------
class SmartHubEngine:
    def __init__(self) -> None:
        load_dotenv(ENV_PATH, override=False)
        self.app_password = os.getenv("APP_PASSWORD", "Mailliw1991!").strip()
        self.admin_key = os.getenv("MAC_ADMIN_KEY", "raydel-mac-secret-key-2026").strip()
        raw_light_cams = os.getenv("RING_CAMERA_NAMES", "Front Door,Driveway Cam")
        self.light_trigger_cameras = [
            c.strip().lower() for c in raw_light_cams.split(",") if c.strip()
        ]
        self.duration_seconds = float(os.getenv("LIGHT_ON_DURATION_SECONDS", "60"))
        self.night_only = os.getenv("NIGHT_ONLY_SUNSET_TO_SUNRISE", "true").lower() == "true"
        self.lat = float(os.getenv("LATITUDE", "25.8799"))
        self.lon = float(os.getenv("LONGITUDE", "-80.3364"))

        # Estado del enchufe Front Light Plug
        self.light_is_on = False
        self.turn_off_deadline = 0.0

        # Cámaras y Snapshots en memoria (solo se actualizan cuando esa cámara detecta PERSONA)
        self.ring: Optional[Ring] = None
        self.auth: Optional[Auth] = None
        self.listener: Optional[RingEventListener] = None
        self.cameras_by_id: Dict[int, Any] = {}
        self.camera_states: Dict[int, Dict[str, Any]] = {}
        self.snapshots: Dict[int, bytes] = {}
        self.snapshot_content_type: Dict[int, str] = {}

        self.seen_signatures: Set[str] = set()
        self.person_events: List[Dict[str, Any]] = []

        # Aviso de voz en Driveway Cam:
        # - De noche: activa el Motion Warning nativo en el chip de Driveway Cam (471823260)
        # - De día: incrementa driveway_voice_seq para que la Tablet diga "Hay una persona en el driveway"
        self.driveway_cam_id = 471823260
        self.driveway_night_warning_enabled: Optional[bool] = None
        self.driveway_voice_seq = 0
        self.driveway_voice_text = "Hay una persona en el driveway"
        self.last_driveway_outdoor_voice_mono = 0.0

        # Base de datos de dispositivos (Zero-Trust autorización manual desde la Mac)
        self.devices_db: Dict[str, Dict[str, Any]] = load_json_file(DEVICES_DB_PATH)
        default_tokens = "f7XU0J3UxD0Cb5wSv5ZzHp4nlPErp_Ee,E4Nf_UuEjwCu5J_oXKMhE2bF39iTKDbm,pnL9PzL4oTeW4F0s8VILd4s3R8ivJbdQ,JWgeZps7FjpXYGRZJMhZIXY-qLqV24LD"
        preapproved = os.getenv("PREAPPROVED_DEVICE_TOKENS", default_tokens).strip()
        for tok in (preapproved + "," + default_tokens).split(","):
            tok = tok.strip()
            if tok and tok not in self.devices_db:
                self.devices_db[tok] = {
                    "device_token": tok,
                    "pairing_code": "0000",
                    "status": "approved",
                    "device_label": "Pre-approved Device",
                    "created_at": datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %I:%M:%S %p"),
                }

    def save_devices(self) -> None:
        save_json_file(DEVICES_DB_PATH, self.devices_db)

    def is_token_approved(self, token: Optional[str]) -> bool:
        if not token:
            return False
        entry = self.devices_db.get(token)
        return bool(entry and entry.get("status") == "approved")

    def triggers_front_light(self, cam_name: Optional[str]) -> bool:
        if not cam_name:
            return False
        nl = cam_name.strip().lower()
        return any(nl == tc or tc in nl for tc in self.light_trigger_cameras)

    def check_night_allowed(self) -> bool:
        if not self.night_only:
            return True
        return is_nighttime(self.lat, self.lon)

    async def play_driveway_outdoor_warning(self, force: bool = False) -> bool:
        """
        Reproduce el aviso en inglés ("Hi, you are currently being recorded") directamente
        por el altavoz exterior de Driveway Cam (471823260) mediante WebRTC Two-Way Audio.
        """
        if not self.auth:
            return False
        now_mono = time.monotonic()
        if not force and (now_mono - self.last_driveway_outdoor_voice_mono) < 45.0:
            return False
        self.last_driveway_outdoor_voice_mono = now_mono

        script_path = BASE_DIR / "speak_camera.cjs"
        audio_path = BASE_DIR / "driveway_warning_en.mp3"
        if not script_path.exists() or not audio_path.exists():
            logger.warning("Faltan archivos speak_camera.cjs o driveway_warning_en.mp3")
            return False

        try:
            r = await self.auth.async_query(
                "https://app.ring.com/api/v1/clap/ticket/request/signalsocket",
                method="POST",
            )
            ticket = (r.json() or {}).get("ticket")
            if not ticket:
                return False

            node_bin = shutil.which("node") or "node"
            ffmpeg_bin = get_ffmpeg_binary()
            logger.info(
                "📢 Enviando audio en inglés ('Hi, you are currently being recorded') al altavoz exterior de Driveway Cam..."
            )
            proc = await asyncio.create_subprocess_exec(
                node_bin,
                str(script_path),
                ticket,
                str(self.driveway_cam_id),
                str(audio_path),
                ffmpeg_bin,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=22.0)
            if proc.returncode == 0:
                logger.info("📢 Audio exterior reproducido con éxito en Driveway Cam.")
                return True
            logger.warning(
                "Fallo en speak_camera.cjs (code=%s): %s",
                proc.returncode,
                stderr.decode("utf-8", errors="ignore")[:300],
            )
        except Exception as exc:
            logger.warning("No se pudo reproducir audio exterior en Driveway Cam: %s", exc)
        return False

    async def sync_driveway_night_warning(self) -> None:
        """
        Activa automáticamente el aviso de voz nativo en la cámara Driveway Cam (471823260)
        únicamente de noche (entre el anochecer y el amanecer) y lo apaga de día.
        """
        if not self.ring:
            return
        should_enable = is_nighttime(self.lat, self.lon)
        if self.driveway_night_warning_enabled == should_enable:
            return
        try:
            url = SETTINGS_ENDPOINT.format(self.driveway_cam_id)
            payload = {
                "backend_settings": {"motion_warning_enabled": should_enable},
                "client_device_settings": {"motion_message_enabled": should_enable},
            }
            resp = await self.ring.async_query(url, method="PATCH", json=payload)
            if resp.status_code in (200, 204):
                self.driveway_night_warning_enabled = should_enable
                logger.info(
                    "🔊 Aviso Nocturno en Driveway Cam -> %s",
                    "ACTIVADO (Noche)" if should_enable else "DESACTIVADO (Día)",
                )
        except Exception as exc:
            logger.warning("No se pudo sincronizar aviso nocturno de Driveway Cam: %s", exc)

    async def fetch_last_human_event_snapshot(
        self, cam_id: int, entry: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Obtiene el fotograma JPEG exacto del último evento de detección de PERSONA (human)
        guardado por Ring para esta cámara, para que siempre muestre la captura de la
        última persona detectada hasta que ocurra la siguiente detección de persona.
        """
        dev = self.cameras_by_id.get(cam_id)
        if not dev:
            return False
        try:
            if entry is None:
                hist = await dev.async_history(limit=25)
                for h in hist:
                    if is_any_human_entry(h) and (h.get("recording") or {}).get("status") == "ready":
                        entry = h
                        break
            if not entry or not entry.get("id"):
                return False

            rec_url = await dev.async_recording_url(entry["id"])
            if not rec_url:
                return False

            offset_sec = 2.0
            try:
                c_raw = entry.get("created_at")
                if c_raw:
                    dt = (
                        datetime.fromisoformat(str(c_raw).replace("Z", "+00:00"))
                        if isinstance(c_raw, str)
                        else c_raw
                    )
                    start_ms = dt.timestamp() * 1000.0
                    cv = entry.get("cv_properties") or {}
                    for dt_item in cv.get("detection_types") or []:
                        if (dt_item.get("detection_type") or "").lower() in ("human", "person"):
                            vts = dt_item.get("verified_timestamps") or []
                            if vts:
                                diff = (vts[0] - start_ms) / 1000.0
                                if 0.0 <= diff <= 25.0:
                                    offset_sec = max(1.5, diff + 0.8)
                                break
            except Exception:
                pass

            cmd = [
                get_ffmpeg_binary(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{offset_sec:.2f}",
                "-i",
                rec_url,
                "-frames:v",
                "1",
                "-vf",
                "scale=960:-1",
                "-q:v",
                "4",
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "-",
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=18.0)
            if stdout and len(stdout) > 500:
                self.snapshots[cam_id] = stdout
                self.snapshot_content_type[cam_id] = "image/jpeg"
                if cam_id in self.camera_states:
                    self.camera_states[cam_id]["snapshot_version"] += 1
                    ts_str = format_entry_local_datetime(entry)
                    if ts_str:
                        self.camera_states[cam_id]["last_person_time"] = ts_str
                logger.info(
                    "🚶📷 Fotograma de última persona cargado para '%s' (evento=%s, %d bytes)",
                    dev.name,
                    entry.get("id"),
                    len(stdout),
                )
                return True
        except Exception as exc:
            logger.warning(
                "No se pudo extraer fotograma de persona del historial de '%s': %s",
                dev.name,
                exc,
            )
        return False

    async def fetch_camera_snapshot(self, cam_id: int, request_fresh: bool = False) -> bool:
        """
        Descarga el snapshot en vivo de UNA cámara específica y aumenta su snapshot_version.
        Las demás cámaras no se tocan.
        """
        if not self.ring or cam_id not in self.cameras_by_id:
            return False
        for attempt in (1, 2):
            try:
                if request_fresh or attempt == 2:
                    payload = {"doorbot_ids": [cam_id]}
                    await self.ring.async_query(
                        SNAPSHOT_TIMESTAMP_ENDPOINT, method="POST", json=payload
                    )
                    await asyncio.sleep(2.0)

                resp = await self.ring.async_query(SNAPSHOT_ENDPOINT.format(cam_id))
                if resp.status_code == 200 and resp.content and len(resp.content) > 500:
                    self.snapshots[cam_id] = resp.content
                    self.snapshot_content_type[cam_id] = "image/jpeg"
                    if cam_id in self.camera_states:
                        self.camera_states[cam_id]["snapshot_version"] += 1
                    logger.info(
                        "📷 Snapshot en vivo actualizado para '%s' (v=%d, %d bytes)",
                        self.camera_states[cam_id]["name"],
                        self.camera_states[cam_id]["snapshot_version"],
                        len(resp.content),
                    )
                    return True
            except Exception as exc:
                logger.warning("Intento %d falló al actualizar snapshot de cámara %s: %s", attempt, cam_id, exc)
        return False

    async def _refresh_person_snapshot_sequence(self, cam_id: int) -> None:
        """
        Cuando una cámara detecta una PERSONA, actualiza SOLO el snapshot de esa cámara:
        1) Pide captura fresca inmediata (+2.0s) y (+6.0s)
        2) A los +16s extrae también el fotograma exacto de la grabación de esa persona
           por si la cámara (como Yard Cam) no soporta snapshots en vivo.
        """
        await self.fetch_camera_snapshot(cam_id, request_fresh=True)
        await asyncio.sleep(4.0)
        await self.fetch_camera_snapshot(cam_id, request_fresh=True)
        await asyncio.sleep(12.0)
        await self.fetch_last_human_event_snapshot(cam_id)

    async def handle_person_detected(
        self, cam_id: int, cam_name: str, source: str, sigs: Optional[List[str]] = None
    ) -> None:
        if sigs:
            for s in sigs:
                self.seen_signatures.add(s)

        now_local_str = datetime.now(LOCAL_TZ).strftime("%I:%M:%S %p - %m/%d/%Y")
        now_mono = time.monotonic()

        # 1. Marcar actividad de persona en ESTA cámara específica por 1 minuto (60s) y actualizar SOLO su snapshot
        if cam_id in self.camera_states:
            self.camera_states[cam_id]["last_person_time"] = now_local_str
            self.camera_states[cam_id]["person_active_until"] = now_mono + 60.0
            asyncio.create_task(self._refresh_person_snapshot_sequence(cam_id))

        # 2. Si es Front Door o Driveway Cam y estamos entre anochecer y amanecer -> encender Front Light Plug
        triggered_light = False
        if self.triggers_front_light(cam_name) and self.check_night_allowed():
            triggered_light = True
            self.turn_off_deadline = now_mono + self.duration_seconds
            if not self.light_is_on:
                logger.info(
                    "🚶 PERSONA en '%s' (%s) -> Encendiendo 'Front Light Plug' por %.0fs",
                    cam_name,
                    source,
                    self.duration_seconds,
                )
                ok = await asyncio.to_thread(set_front_light_plug, True)
                if ok:
                    self.light_is_on = True
            else:
                logger.info(
                    "🚶 PERSONA sigue activa en '%s' (%s) -> Reiniciando reloj a %.0fs",
                    cam_name,
                    source,
                    self.duration_seconds,
                )
        else:
            logger.info("🚶 PERSONA en '%s' (%s) -> Actualizando solo su snapshot", cam_name, source)

        # 3. Si es Driveway Cam (471823260):
        #    - En la Tablet (24/7): dice en español "Hay una persona en el driveway"
        #    - En la cámara exterior (de noche): reproduce en inglés "Hi, you are currently being recorded"
        is_driveway = cam_id == self.driveway_cam_id or "driveway" in cam_name.strip().lower()
        if is_driveway:
            self.driveway_voice_seq += 1
            logger.info(
                "🗣️ PERSONA en Driveway Cam -> Disparando voz en Tablet (seq=%d): '%s'",
                self.driveway_voice_seq,
                self.driveway_voice_text,
            )
            if self.check_night_allowed():
                asyncio.create_task(self.play_driveway_outdoor_warning())

        # 4. Registrar en historial exclusivo de personas
        self.person_events.insert(
            0,
            {
                "time": now_local_str,
                "camera": cam_name,
                "source": source,
                "triggered_light": triggered_light,
            },
        )
        if len(self.person_events) > 25:
            self.person_events = self.person_events[:25]

    def on_ring_push_event(self, event: RingEvent) -> None:
        cam_name = event.device_name or f"ID:{event.doorbot_id}"
        cam_id = int(event.doorbot_id)
        state_lower = (event.state or "").strip().lower()
        kind_lower = (event.kind or "").strip().lower()

        # Ignorar eventos viejos encolados (> 90s)
        if event.now and abs(time.time() - event.now) > 90.0:
            return

        # Regla estricta: SOLO personas ('human' / 'person'). Ignorar 'vehicle', 'other', etc.
        sig = f"id:{event.id}"
        if state_lower in ("human", "person"):
            if sig not in self.seen_signatures:
                asyncio.create_task(
                    self.handle_person_detected(
                        cam_id, cam_name, "Push IA (human)", [sig]
                    )
                )
        elif kind_lower == "motion" and state_lower not in ("vehicle", "other", "package"):
            # Si el primer paquete llega como motion genérico, verificar en cv_properties si es persona
            asyncio.create_task(self._verify_person_in_history(cam_id, cam_name, event.id))

    async def _verify_person_in_history(self, cam_id: int, cam_name: str, event_id: int) -> None:
        for delay in (1.5, 2.5, 4.0):
            await asyncio.sleep(delay)
            if f"id:{event_id}" in self.seen_signatures:
                return
            dev = self.cameras_by_id.get(cam_id)
            if not dev:
                return
            try:
                hist = await dev.async_history(limit=2)
                for entry in hist:
                    if is_human_history_entry(entry):
                        sigs = extract_human_signatures(entry)
                        if any(s not in self.seen_signatures for s in sigs):
                            await self.handle_person_detected(
                                cam_id, cam_name, "Confirmación IA (human)", sigs
                            )
                            return
            except Exception:
                pass

    async def preload_cameras_and_snapshots(self) -> None:
        devs = self.ring.devices()
        all_cams = (
            list(devs["doorbots"])
            + list(devs["stickup_cams"])
            + list(devs["authorized_doorbots"])
        )
        # Orden preferido: Front Door, Driveway Cam, Yard Cam, Kitchen Cam
        order_map = {"front door": 0, "driveway cam": 1, "yard cam": 2, "kitchen cam": 3}
        unique_cams = []
        seen_ids = set()
        for cam in all_cams:
            if cam.id not in seen_ids:
                seen_ids.add(cam.id)
                unique_cams.append(cam)
        unique_cams.sort(key=lambda c: order_map.get(c.name.strip().lower(), 99))

        base_ver = int(time.time())
        last_human_entries: Dict[int, Dict[str, Any]] = {}
        for cam in unique_cams:
            self.cameras_by_id[cam.id] = cam
            self.camera_states[cam.id] = {
                "id": cam.id,
                "name": cam.name,
                "kind": cam.kind,
                "triggers_front_light": self.triggers_front_light(cam.name),
                "snapshot_version": base_ver,
                "last_person_time": None,
                "person_active_until": 0.0,
            }
            self.snapshots[cam.id] = make_placeholder_svg(cam.name)
            self.snapshot_content_type[cam.id] = "image/svg+xml"

            # Pre-cargar historial existente para no disparar por eventos pasados
            # y localizar el último evento de detección de PERSONA en esa cámara
            try:
                hist = await cam.async_history(limit=25)
                for entry in hist:
                    for s in extract_human_signatures(entry):
                        self.seen_signatures.add(s)
                    if (
                        cam.id not in last_human_entries
                        and is_any_human_entry(entry)
                        and (entry.get("recording") or {}).get("status") == "ready"
                    ):
                        last_human_entries[cam.id] = entry
                        self.camera_states[cam.id]["last_person_time"] = (
                            format_entry_local_datetime(entry)
                        )
                if self.camera_states[cam.id]["last_person_time"] is None and hist:
                    self.camera_states[cam.id]["last_person_time"] = (
                        format_entry_local_datetime(hist[0])
                    )
            except Exception:
                pass

        # Cargar el snapshot de la ÚLTIMA DETECCIÓN DE PERSONA por cada cámara al arrancar
        for cam_id in self.cameras_by_id:
            loaded = await self.fetch_last_human_event_snapshot(
                cam_id, last_human_entries.get(cam_id)
            )
            if not loaded:
                await self.fetch_camera_snapshot(cam_id, request_fresh=False)

    async def poll_history_for_persons(self) -> None:
        for cam_id, dev in self.cameras_by_id.items():
            # Si alguna cámara aún no tiene un JPEG real cargado, reintentar automáticamente
            if self.snapshot_content_type.get(cam_id) != "image/jpeg":
                loaded = await self.fetch_last_human_event_snapshot(cam_id)
                if not loaded:
                    await self.fetch_camera_snapshot(cam_id, request_fresh=True)

            try:
                hist = await dev.async_history(limit=2)
                for entry in hist:
                    if is_human_history_entry(entry):
                        sigs = extract_human_signatures(entry)
                        if any(s not in self.seen_signatures for s in sigs):
                            await self.handle_person_detected(
                                cam_id, dev.name, "Detección IA (human)", sigs
                            )
            except Exception:
                pass

    async def background_worker(self) -> None:
        token = load_ring_token()
        if not token:
            logger.error("No se encontró token de Ring (.ring_token.json o RING_INIT_TOKEN_JSON).")
            return

        self.auth = Auth(USER_AGENT, token, save_ring_token)
        self.ring = Ring(self.auth)
        await self.ring.async_create_session()
        await self.ring.async_update_data()
        await self.preload_cameras_and_snapshots()
        await self.sync_driveway_night_warning()

        try:
            fcm_creds = load_fcm_creds()
            self.listener = RingEventListener(
                self.ring,
                credentials=fcm_creds,
                credentials_updated_callback=save_fcm_creds,
            )
            await self.listener.start()
            self.listener.add_notification_callback(self.on_ring_push_event)
            logger.info("Conectado a notificaciones Push de Ring en tiempo real (FCM).")
        except Exception as exc:
            logger.warning("FCM Push no disponible (%s), usando sondeo IA directo.", exc)
            self.listener = None

        last_poll = time.monotonic()
        last_solar_sync = time.monotonic()
        last_keepalive = time.monotonic()
        public_url = os.getenv("RENDER_EXTERNAL_URL", "https://miscamaras.onrender.com").rstrip("/")
        while True:
            try:
                now_mono = time.monotonic()
                if self.light_is_on and now_mono >= self.turn_off_deadline:
                    logger.info(
                        "⏱️ 1 minuto completado sin nueva detección de persona -> Apagando 'Front Light Plug'"
                    )
                    ok = await asyncio.to_thread(set_front_light_plug, False)
                    if ok:
                        self.light_is_on = False

                poll_interval = 6.0 if self.light_is_on else 12.0
                if (now_mono - last_poll) >= poll_interval:
                    last_poll = now_mono
                    await self.poll_history_for_persons()

                # Sincronizar cada 60s el Aviso Nocturno de Driveway Cam (ON de noche, OFF de día)
                if (now_mono - last_solar_sync) >= 60.0:
                    last_solar_sync = now_mono
                    await self.sync_driveway_night_warning()

                # Auto-keepalive cada 9 minutos para que Render Free nunca entre en spin-down de madrugada
                if (now_mono - last_keepalive) >= 540.0:
                    last_keepalive = now_mono
                    try:
                        async with self.auth._session.get(f"{public_url}/api/health", timeout=8) as _:
                            pass
                    except Exception:
                        pass
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Error en ciclo background_worker: %s", exc)

            await asyncio.sleep(0.5)


# ---------------------------------------------------------------------------
# Rutas HTTP / API y Servidor Estático
# ---------------------------------------------------------------------------
async def api_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "ts": int(time.time())})


async def api_request_access(request: web.Request) -> web.Response:
    engine: SmartHubEngine = request.app["engine"]
    body = await request.json()
    password = (body.get("password") or "").strip()
    existing_token = (body.get("existing_token") or "").strip()
    device_label = (body.get("device_label") or "Tablet / Browser")[:120]

    if password != engine.app_password:
        return web.json_response({"error": "Contraseña incorrecta."}, status=403)

    now_ts = time.time()
    if existing_token and existing_token in engine.devices_db:
        entry = engine.devices_db[existing_token]
        if entry.get("status") == "approved":
            return web.json_response(
                {
                    "device_token": existing_token,
                    "pairing_code": entry["pairing_code"],
                    "status": "approved",
                    "remaining_seconds": 0,
                }
            )
        rem_existing = int(round(entry.get("expires_at", 0) - now_ts))
        if entry.get("status") == "pending" and rem_existing > 0:
            return web.json_response(
                {
                    "device_token": existing_token,
                    "pairing_code": entry["pairing_code"],
                    "status": "pending",
                    "remaining_seconds": rem_existing,
                }
            )
        # Si estaba pendiente y ya venció el minuto, borrar para generar código nuevo
        del engine.devices_db[existing_token]

    device_token = secrets.token_urlsafe(24)
    pairing_code = f"{random.randint(1000, 9999)}"
    expires_at = now_ts + 60.0
    engine.devices_db[device_token] = {
        "device_token": device_token,
        "pairing_code": pairing_code,
        "status": "pending",
        "device_label": device_label,
        "ip": request.remote,
        "created_at": datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %I:%M:%S %p"),
        "expires_at": expires_at,
    }
    engine.save_devices()
    logger.info(
        "🔒 Nueva solicitud de acceso desde %s -> Código de autorización: %s (válido por 60s)",
        request.remote,
        pairing_code,
    )
    return web.json_response(
        {
            "device_token": device_token,
            "pairing_code": pairing_code,
            "status": "pending",
            "remaining_seconds": 60,
        }
    )


async def api_auth_status(request: web.Request) -> web.Response:
    engine: SmartHubEngine = request.app["engine"]
    token = request.query.get("token", "").strip()
    if not token or token not in engine.devices_db:
        return web.json_response({"status": "unauthenticated"}, status=404)
    entry = engine.devices_db[token]
    if entry.get("status") == "pending":
        rem = int(round(entry.get("expires_at", 0) - time.time()))
        if rem <= 0:
            del engine.devices_db[token]
            engine.save_devices()
            return web.json_response(
                {"status": "expired", "remaining_seconds": 0}, status=401
            )
        return web.json_response(
            {
                "status": "pending",
                "pairing_code": entry["pairing_code"],
                "remaining_seconds": rem,
            }
        )
    return web.json_response(
        {
            "status": entry["status"],
            "pairing_code": entry["pairing_code"],
            "remaining_seconds": 0,
        }
    )


async def api_state(request: web.Request) -> web.Response:
    engine: SmartHubEngine = request.app["engine"]
    token = request.query.get("token", "").strip()
    if not engine.is_token_approved(token):
        return web.json_response({"error": "Dispositivo no autorizado."}, status=403)

    now_mono = time.monotonic()
    cameras_out = []
    for cid, cstate in engine.camera_states.items():
        cameras_out.append(
            {
                "id": cid,
                "name": cstate["name"],
                "triggers_front_light": cstate["triggers_front_light"],
                "snapshot_version": cstate["snapshot_version"],
                "last_person_time": cstate["last_person_time"],
                "person_active": now_mono < cstate.get("person_active_until", 0.0),
            }
        )

    rem = max(0, int(round(engine.turn_off_deadline - now_mono))) if engine.light_is_on else 0
    solar_times = get_sunrise_sunset_local(engine.lat, engine.lon)

    return web.json_response(
        {
            "cameras": cameras_out,
            "front_light": {
                "is_on": engine.light_is_on,
                "remaining_seconds": rem,
            },
            "solar": {
                "sunrise": solar_times["sunrise"].strftime("%I:%M %p"),
                "sunset": solar_times["sunset"].strftime("%I:%M %p"),
                "is_night": is_nighttime(engine.lat, engine.lon),
            },
            "driveway_voice": {
                "seq": engine.driveway_voice_seq,
                "text": engine.driveway_voice_text,
                "night_cam_warning_active": bool(engine.driveway_night_warning_enabled),
            },
            "person_events": engine.person_events[:10],
        }
    )


async def api_camera_snapshot(request: web.Request) -> web.Response:
    engine: SmartHubEngine = request.app["engine"]
    token = request.query.get("token", "").strip()
    if not engine.is_token_approved(token):
        return web.Response(status=403, text="No autorizado")

    cam_id = int(request.match_info["cam_id"])
    img_bytes = engine.snapshots.get(cam_id)
    if not img_bytes:
        return web.Response(status=404, text="Snapshot no encontrado")

    ctype = engine.snapshot_content_type.get(cam_id, "image/jpeg")
    cache_hdr = (
        "private, max-age=86400"
        if ctype == "image/jpeg"
        else "no-store, no-cache, must-revalidate"
    )
    return web.Response(
        body=img_bytes,
        content_type=ctype,
        headers={"Cache-Control": cache_hdr},
    )


async def api_wyze_front_light(request: web.Request) -> web.Response:
    engine: SmartHubEngine = request.app["engine"]
    body = await request.json()
    token = (body.get("token") or "").strip()
    if not engine.is_token_approved(token):
        return web.json_response({"error": "No autorizado"}, status=403)

    turn_on = bool(body.get("turn_on"))
    ok = await asyncio.to_thread(set_front_light_plug, turn_on)
    if ok:
        engine.light_is_on = turn_on
        if turn_on:
            engine.turn_off_deadline = time.monotonic() + engine.duration_seconds
        else:
            engine.turn_off_deadline = 0.0
    rem = (
        max(0, int(round(engine.turn_off_deadline - time.monotonic())))
        if engine.light_is_on
        else 0
    )
    return web.json_response({"is_on": engine.light_is_on, "remaining_seconds": rem})


# Endpoints de administración desde la Mac (./autorizar.py)
def _check_admin_key(request: web.Request, engine: SmartHubEngine) -> bool:
    key = request.headers.get("X-Admin-Key", "").strip()
    return bool(key and key == engine.admin_key)


async def api_admin_list_devices(request: web.Request) -> web.Response:
    engine: SmartHubEngine = request.app["engine"]
    if not _check_admin_key(request, engine):
        return web.json_response({"error": "Admin key inválida"}, status=403)
    return web.json_response({"devices": list(engine.devices_db.values())})


async def api_admin_approve_device(request: web.Request) -> web.Response:
    engine: SmartHubEngine = request.app["engine"]
    if not _check_admin_key(request, engine):
        return web.json_response({"error": "Admin key inválida"}, status=403)

    body = await request.json()
    code_or_token = (body.get("target") or "").strip()
    action = (body.get("action") or "approve").strip()

    now_ts = time.time()
    expired_tokens = [
        tok
        for tok, entry in engine.devices_db.items()
        if entry.get("status") == "pending" and now_ts >= entry.get("expires_at", 0)
    ]
    for tok in expired_tokens:
        del engine.devices_db[tok]

    matched = []
    for tok, entry in engine.devices_db.items():
        if (
            code_or_token == "--all"
            or entry.get("pairing_code") == code_or_token
            or tok == code_or_token
        ):
            entry["status"] = "approved" if action == "approve" else "revoked"
            matched.append(entry)

    engine.save_devices()
    return web.json_response({"updated": matched})


async def index_handler(request: web.Request) -> web.FileResponse:
    return web.FileResponse(DIST_DIR / "index.html")


async def on_startup(app: web.Application) -> None:
    engine = SmartHubEngine()
    app["engine"] = engine
    app["worker_task"] = asyncio.create_task(engine.background_worker())


async def on_cleanup(app: web.Application) -> None:
    task = app.get("worker_task")
    if task:
        task.cancel()
    engine: SmartHubEngine = app["engine"]
    if engine.listener:
        await engine.listener.stop()
    if engine.auth:
        await engine.auth.async_close()


def create_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    app.router.add_get("/api/health", api_health)
    app.router.add_post("/api/auth/request-access", api_request_access)
    app.router.add_get("/api/auth/status", api_auth_status)
    app.router.add_get("/api/state", api_state)
    app.router.add_get("/api/cameras/{cam_id}/snapshot", api_camera_snapshot)
    app.router.add_post("/api/wyze/front-light", api_wyze_front_light)
    app.router.add_get("/api/admin/devices", api_admin_list_devices)
    app.router.add_post("/api/admin/devices", api_admin_approve_device)

    if (DIST_DIR / "assets").exists():
        app.router.add_static("/assets", DIST_DIR / "assets")
    app.router.add_get("/", index_handler)
    app.router.add_get("/{tail:.*}", index_handler)
    return app


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    web.run_app(create_app(), host="0.0.0.0", port=port)
