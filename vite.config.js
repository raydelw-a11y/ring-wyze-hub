import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const now = new Date();
const timePart = now.toLocaleTimeString('en-US', {
  timeZone: 'America/New_York',
  hour: '2-digit',
  minute: '2-digit',
  hour12: true,
});
const datePart = now.toLocaleDateString('en-US', {
  timeZone: 'America/New_York',
  month: '2-digit',
  day: '2-digit',
  year: 'numeric',
});
const buildTimestamp = `Actualizado: ${timePart} - ${datePart}`;

export default defineConfig({
  plugins: [react()],
  define: {
    __BUILD_TIMESTAMP__: JSON.stringify(buildTimestamp),
  },
  server: {
    proxy: {
      '/api': 'http://localhost:8080',
    },
  },
});
