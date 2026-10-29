#!/usr/bin/env node
import { firstValueFrom, timer, race } from 'rxjs';
import { WebrtcConnection } from './node_modules/ring-client-api/lib/streaming/webrtc-connection.js';
import { StreamingSession } from './node_modules/ring-client-api/lib/streaming/streaming-session.js';
import { setFfmpegPath } from './node_modules/ring-client-api/lib/ffmpeg.js';

const [, , ticket, camIdRaw, audioPath, ffmpegBin] = process.argv;
if (!ticket || !camIdRaw || !audioPath) {
  console.error('Usage: node speak_camera.mjs <ticket> <cam_id> <audio_path> [ffmpeg_bin]');
  process.exit(1);
}

if (ffmpegBin) {
  setFfmpegPath(ffmpegBin);
}

const camera = {
  id: Number(camIdRaw),
  name: `Cam-${camIdRaw}`,
  isRingEdgeEnabled: false,
};

async function run() {
  const conn = new WebrtcConnection(ticket, camera, {});
  const session = new StreamingSession(camera, conn);

  let sentPackets = 0;
  const origSend = conn.sendAudioPacket.bind(conn);
  conn.sendAudioPacket = (rtp) => {
    sentPackets++;
    return origSend(rtp);
  };

  const unmuteSpeaker = () => {
    try {
      conn.sendSessionMessage('camera_options', { stealth_mode: false });
      conn.sendSessionMessage('stream_options', { audio_enabled: true, video_enabled: true });
    } catch (_) {}
  };

  conn.onCallAnswered.subscribe(() => console.log('SDP Answered by Ring'));
  conn.onCameraConnected.subscribe(() => {
    console.log('Camera Connected! Activating speaker...');
    unmuteSpeaker();
  });

  conn.onMessage.subscribe((msg) => {
    if (msg && (msg.method === 'camera_started' || msg.method === 'stream_info')) {
      console.log(`Received ${msg.method} -> ensuring speaker stealth_mode=false`);
      unmuteSpeaker();
    }
  });

  // Activar el altavoz exterior de la cámara (stealth_mode: false)
  session.activateCameraSpeaker();

  // Esperar a que la cámara conecte el canal WebRTC (máx 12s)
  await firstValueFrom(race(conn.onCameraConnected, timer(12000)));
  if (session.hasEnded) {
    console.error('Session ended before audio could start');
    process.exit(2);
  }

  // Pausa de 1000ms para que camera_started llegue y el amplificador físico abra el altavoz
  await new Promise((r) => setTimeout(r, 1000));
  unmuteSpeaker();

  console.log('Streaming English warning audio to camera speaker...');
  await session.transcodeReturnAudio({
    input: [audioPath],
  });

  // Esperar a que termine de reproducirse el audio (~7.2s) o máx 11s
  await firstValueFrom(race(session.onCallEnded, timer(11000)));
  console.log(`Audio playback complete (sent ${sentPackets} RTP packets), closing session.`);
  session.stop();

  if (sentPackets < 20) {
    console.error(`Too few RTP packets sent (${sentPackets})`);
    process.exit(2);
  }
  process.exit(0);
}

run().catch((err) => {
  console.error('Error en speak_camera:', err);
  process.exit(1);
});
