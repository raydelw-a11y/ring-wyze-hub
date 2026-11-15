#!/usr/bin/env bash
set -euo pipefail

# Instala raydel-global-rules en ~/.gemini/config/plugins/raydel-global-rules
# en cualquier máquina nueva, contenedor o servidor SSH remoto.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_PLUGIN_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEST_PLUGIN_DIR="${HOME}/.gemini/config/plugins/raydel-global-rules"
BACKUP_DIR="${HOME}/.gemini/backups"

RULE_BYTES=$(wc -c < "${SRC_PLUGIN_DIR}/rules/AGENTS.md" | tr -d ' ')
if [ "${RULE_BYTES}" -ge 24000 ]; then
  echo "❌ Error: rules/AGENTS.md (${RULE_BYTES} bytes) supera el límite de 24,000 bytes de Antigravity." >&2
  exit 1
fi

if [ -f "${HOME}/.gemini/config/GEMINI.md" ]; then
  mkdir -p "${BACKUP_DIR}"
  TS="$(date +%Y-%m-%d_%H%M%S)"
  mv "${HOME}/.gemini/config/GEMINI.md" "${BACKUP_DIR}/GEMINI.md.${TS}.bak"
  echo "📦 Respaldo de ~/.gemini/config/GEMINI.md guardado en ${BACKUP_DIR}/GEMINI.md.${TS}.bak"
fi

mkdir -p "${DEST_PLUGIN_DIR}/rules" "${DEST_PLUGIN_DIR}/scripts"
cp "${SRC_PLUGIN_DIR}/plugin.json" "${DEST_PLUGIN_DIR}/plugin.json"
cp "${SRC_PLUGIN_DIR}/rules/AGENTS.md" "${DEST_PLUGIN_DIR}/rules/AGENTS.md"
cp "${SRC_PLUGIN_DIR}/README.md" "${DEST_PLUGIN_DIR}/README.md"
cp "${SRC_PLUGIN_DIR}/scripts/install-global.sh" "${DEST_PLUGIN_DIR}/scripts/install-global.sh"
if [ -f "${SRC_PLUGIN_DIR}/scripts/sync-all-repos.sh" ]; then
  cp "${SRC_PLUGIN_DIR}/scripts/sync-all-repos.sh" "${DEST_PLUGIN_DIR}/scripts/sync-all-repos.sh"
fi
chmod +x "${DEST_PLUGIN_DIR}/scripts/"*.sh

echo "✅ raydel-global-rules instalado globalmente en ${DEST_PLUGIN_DIR} (${RULE_BYTES} bytes < 24,000 bytes)."
