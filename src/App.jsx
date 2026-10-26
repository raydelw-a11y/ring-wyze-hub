import React, { useState, useEffect, useCallback, useRef } from 'react';

/* global __BUILD_TIMESTAMP__ */
const FALLBACK_TIMESTAMP = 'Actualizado: 04:40 PM - 10/26/2026';
const BUILD_TIMESTAMP =
  typeof __BUILD_TIMESTAMP__ !== 'undefined' ? __BUILD_TIMESTAMP__ : FALLBACK_TIMESTAMP;

// Reproductor de alerta hablada en español para la Tablet ("Hay una persona en el driveway")
function speakSpanishAlert(text = 'Hay una persona en el driveway') {
  try {
    // 1. Campanilla corta de atención (Web Audio API)
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (AudioCtx) {
      const ctx = new AudioCtx();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sine';
      osc.frequency.setValueAtTime(660, ctx.currentTime);
      osc.frequency.setValueAtTime(880, ctx.currentTime + 0.14);
      gain.gain.setValueAtTime(0.18, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.35);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start();
      osc.stop(ctx.currentTime + 0.36);
    }
  } catch {
    // Ignorar si Web Audio no está disponible
  }

  try {
    // 2. Síntesis de voz en español (Web Speech API)
    if ('speechSynthesis' in window) {
      window.speechSynthesis.cancel();
      const utter = new SpeechSynthesisUtterance(text);
      utter.lang = 'es-US';
      utter.rate = 0.98;
      utter.pitch = 1.0;
      utter.volume = 1.0;

      const voices = window.speechSynthesis.getVoices() || [];
      const esVoice =
        voices.find((v) => v.lang && v.lang.toLowerCase().startsWith('es-us')) ||
        voices.find((v) => v.lang && v.lang.toLowerCase().startsWith('es-mx')) ||
        voices.find((v) => v.lang && v.lang.toLowerCase().startsWith('es'));
      if (esVoice) {
        utter.voice = esVoice;
      }

      window.speechSynthesis.speak(utter);
    }
  } catch {
    // Ignorar errores de síntesis
  }
}

const CameraCard = React.memo(function CameraCard({ cam, deviceToken, isNight }) {
  const imgUrl = `/api/cameras/${cam.id}/snapshot?v=${cam.snapshot_version}&token=${encodeURIComponent(
    deviceToken
  )}`;
  const isDriveway =
    cam.id === 471823260 || (cam.name || '').toLowerCase().includes('driveway');

  return (
    <div className={`camera-card ${cam.person_active ? 'person-active' : ''}`}>
      <div className="camera-header">
        <div className="camera-title">
          <span>📹 {cam.name}</span>
          {cam.triggers_front_light && (
            <span className="auto-light-tag">💡 Luz Automática</span>
          )}
          {isDriveway && (
            <span className="auto-light-tag" style={{ color: '#fdba74', borderColor: 'rgba(251, 146, 60, 0.3)', background: 'rgba(251, 146, 60, 0.12)' }}>
              {isNight ? '🔊 Voz en Cámara (Noche)' : '🗣️ Voz en Tablet (Día)'}
            </span>
          )}
        </div>
        <div>
          <span className="person-badge">
            🚶 PERSONA DETECTADA{cam.last_person_time ? ` • ${cam.last_person_time}` : ''}
          </span>
        </div>
      </div>

      <div className="camera-Viewport">
        <img src={imgUrl} alt={cam.name} loading="eager" />
      </div>
    </div>
  );
});

export default function App() {
  const [password, setPassword] = useState('');
  const [authError, setAuthError] = useState('');
  const [deviceToken, setDeviceToken] = useState(
    () => localStorage.getItem('rw_device_token') || ''
  );
  const [pairingCode, setPairingCode] = useState(
    () => localStorage.getItem('rw_pairing_code') || ''
  );
  const [authStatus, setAuthStatus] = useState('checking'); // 'unauthenticated' | 'pending' | 'approved'
  const [hubState, setHubState] = useState(null);
  const [togglingPlug, setTogglingPlug] = useState(false);
  const lastSpokenSeqRef = useRef(null);

  // Precargar voces del navegador y desbloquear síntesis al primer toque en la pantalla de la Tablet
  useEffect(() => {
    if ('speechSynthesis' in window) {
      window.speechSynthesis.getVoices();
    }
    const unlockAudio = () => {
      try {
        if ('speechSynthesis' in window) {
          const silent = new SpeechSynthesisUtterance('');
          silent.volume = 0;
          window.speechSynthesis.speak(silent);
        }
      } catch {
        // Ignorar
      }
      window.removeEventListener('pointerdown', unlockAudio);
      window.removeEventListener('keydown', unlockAudio);
    };
    window.addEventListener('pointerdown', unlockAudio, { passive: true });
    window.addEventListener('keydown', unlockAudio, { passive: true });
    return () => {
      window.removeEventListener('pointerdown', unlockAudio);
      window.removeEventListener('keydown', unlockAudio);
    };
  }, []);

  const checkDeviceStatus = useCallback(async (tokenToCheck) => {
    if (!tokenToCheck) {
      setAuthStatus('unauthenticated');
      return;
    }
    try {
      const res = await fetch(
        `/api/auth/status?token=${encodeURIComponent(tokenToCheck)}`
      );
      if (res.status === 401 || res.status === 404) {
        localStorage.removeItem('rw_device_token');
        localStorage.removeItem('rw_pairing_code');
        setDeviceToken('');
        setPairingCode('');
        setAuthStatus('unauthenticated');
        return;
      }
      const data = await res.json();
      if (data.status === 'approved') {
        setAuthStatus('approved');
      } else if (data.status === 'pending') {
        setPairingCode(data.pairing_code || '');
        setAuthStatus('pending');
      } else {
        setAuthStatus('unauthenticated');
      }
    } catch {
      // Si hay corte momentáneo de red, mantener estado actual
    }
  }, []);

  useEffect(() => {
    checkDeviceStatus(deviceToken);
  }, [deviceToken, checkDeviceStatus]);

  // Si está pendiente de autorización desde la Mac, consultar cada 2s
  useEffect(() => {
    if (authStatus !== 'pending' || !deviceToken) return;
    const timer = setInterval(() => {
      checkDeviceStatus(deviceToken);
    }, 2000);
    return () => clearInterval(timer);
  }, [authStatus, deviceToken, checkDeviceStatus]);

  // Una vez autorizado, consultar el estado ligero (JSON ~300 bytes) cada 2s
  useEffect(() => {
    if (authStatus !== 'approved' || !deviceToken) return;
    let active = true;
    const fetchState = async () => {
      try {
        const res = await fetch(
          `/api/state?token=${encodeURIComponent(deviceToken)}`
        );
        if (res.status === 401 || res.status === 403) {
          if (active) checkDeviceStatus(deviceToken);
          return;
        }
        if (res.ok) {
          const data = await res.json();
          if (active) {
            const nextSeq = data.driveway_voice?.seq ?? 0;
            const voiceText =
              data.driveway_voice?.text || 'Hay una persona en el driveway';
            if (lastSpokenSeqRef.current === null) {
              lastSpokenSeqRef.current = nextSeq;
            } else if (nextSeq > lastSpokenSeqRef.current) {
              lastSpokenSeqRef.current = nextSeq;
              speakSpanishAlert(voiceText);
            }
            setHubState(data);
          }
        }
      } catch {
        // Ignorar errores transitorios
      }
    };
    fetchState();
    const timer = setInterval(fetchState, 2000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [authStatus, deviceToken, checkDeviceStatus]);

  const handlePasswordSubmit = async (e) => {
    if (e) e.preventDefault();
    setAuthError('');
    if (!password.trim()) {
      setAuthError('Ingresa la contraseña de acceso privado.');
      return;
    }
    try {
      const res = await fetch('/api/auth/request-access', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          password: password.trim(),
          existing_token: deviceToken,
          device_label: navigator.userAgent,
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        setAuthError(data.error || 'Contraseña incorrecta.');
        return;
      }
      localStorage.setItem('rw_device_token', data.device_token);
      localStorage.setItem('rw_pairing_code', data.pairing_code);
      setDeviceToken(data.device_token);
      setPairingCode(data.pairing_code);
      setAuthStatus(data.status);
    } catch {
      setAuthError('No se pudo conectar con el servidor.');
    }
  };

  const handleToggleFrontLight = async () => {
    if (!hubState || togglingPlug) return;
    setTogglingPlug(true);
    try {
      const nextOn = !hubState.front_light.is_on;
      const res = await fetch('/api/wyze/front-light', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          token: deviceToken,
          turn_on: nextOn,
        }),
      });
      if (res.ok) {
        const data = await res.json();
        setHubState((prev) =>
          prev
            ? {
                ...prev,
                front_light: {
                  ...prev.front_light,
                  is_on: data.is_on,
                  remaining_seconds: data.remaining_seconds,
                },
              }
            : prev
        );
      }
    } finally {
      setTogglingPlug(false);
    }
  };

  // 1. Pantalla de Contraseña (Acceso Privado)
  if (authStatus === 'checking' || authStatus === 'unauthenticated') {
    return (
      <div className="auth-screen">
        <div className="auth-card">
          <div className="auth-icon">🔐</div>
          <h1>Acceso Privado</h1>
          <p>
            Panel 24/7 de Cámaras Ring (Solo Personas) y Automatización Wyze Front Light.
          </p>
          <form onSubmit={handlePasswordSubmit}>
            <input
              type="password"
              className="auth-input"
              placeholder="Contraseña de acceso..."
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  handlePasswordSubmit(e);
                }
              }}
              autoFocus
            />
            {authError && <div className="auth-error">{authError}</div>}
            <button type="submit" className="auth-btn">
              Continuar
            </button>
          </form>
        </div>
        <div className="build-timestamp-badge">{BUILD_TIMESTAMP}</div>
      </div>
    );
  }

  // 2. Pantalla de Espera de Autorización Manual desde la Mac
  if (authStatus === 'pending') {
    return (
      <div className="auth-screen">
        <div className="auth-card">
          <div className="auth-icon">🛡️</div>
          <h1>Autorización Manual Requerida</h1>
          <p>
            Por seguridad, este dispositivo debe ser autorizado manualmente desde tu Mac antes de mostrar las cámaras.
          </p>
          <div className="pairing-code-box">
            <div style={{ fontSize: 12, color: '#94a3b8', marginBottom: 4 }}>
              CÓDIGO DE ESTE DISPOSITIVO
            </div>
            <div className="pairing-code">{pairingCode}</div>
          </div>
          <p style={{ marginBottom: 8 }}>
            Ejecuta en tu Mac para autorizar esta pantalla:
          </p>
          <div className="cmd-hint">./autorizar.py {pairingCode}</div>
        </div>
        <div className="build-timestamp-badge">{BUILD_TIMESTAMP}</div>
      </div>
    );
  }

  // 3. Pantalla Principal del Panel 24/7 (4 Cámaras en Pantalla Completa sin Scroll)
  const cameras = hubState?.cameras || [];
  const frontLight = hubState?.front_light || { is_on: false, remaining_seconds: 0 };
  const solar = hubState?.solar || {};

  return (
    <div className="dashboard">
      <header className="top-bar">
        <div className="brand">
          <span style={{ fontSize: 20 }}>🏠</span>
          <div>
            <h2>Ring + Wyze Smart Hub 24/7</h2>
            <div style={{ fontSize: 11.5, color: '#94a3b8' }}>
              Filtro exclusivo de personas (IA) • {solar.is_night ? '🌙 Modo Nocturno Activo' : '☀️ Modo Diurno'}{' '}
              {solar.sunset && solar.sunrise ? `(Anochecer ${solar.sunset} / Amanecer ${solar.sunrise})` : ''}
            </div>
          </div>
        </div>

        <div className="status-pills">
          <button
            type="button"
            onClick={() => speakSpanishAlert('Hay una persona en el driveway')}
            className="toggle-btn"
            title="Probar y activar voz en español en la Tablet"
          >
            🔊 Probar Voz Tablet
          </button>

          <div className={`pill ${frontLight.is_on ? 'light-on' : 'online'}`}>
            <span>💡 Front Light Plug:</span>
            <strong>{frontLight.is_on ? 'ENCENDIDO' : 'APAGADO'}</strong>
            {frontLight.is_on && frontLight.remaining_seconds > 0 && (
              <span>({frontLight.remaining_seconds}s)</span>
            )}
          </div>

          <button
            type="button"
            onClick={handleToggleFrontLight}
            disabled={togglingPlug}
            className={`toggle-btn ${frontLight.is_on ? 'active' : ''}`}
          >
            {frontLight.is_on ? 'Apagar Luz' : 'Encender 1 min'}
          </button>
        </div>
      </header>

      <main className="cameras-grid">
        {cameras.map((cam) => (
          <CameraCard
            key={cam.id}
            cam={cam}
            deviceToken={deviceToken}
            isNight={Boolean(solar.is_night)}
          />
        ))}
      </main>

      <div className="build-timestamp-badge">{BUILD_TIMESTAMP}</div>
    </div>
  );
}
