#!/usr/bin/env bash
set -euo pipefail

# Propaga ~/.gemini/config/plugins/raydel-global-rules hacia .agents/plugins/raydel-global-rules
# en todos los repositorios del usuario usando GIT_INDEX_FILE efímero (0 contaminación de árboles sucios)
# y hace git push en los repos con remoto origin.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GLOBAL_PLUGIN_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PUSH_REMOTES="${PUSH_REMOTES:-1}"

if [ ! -f "${GLOBAL_PLUGIN_DIR}/plugin.json" ] || [ ! -f "${GLOBAL_PLUGIN_DIR}/rules/AGENTS.md" ]; then
  echo "❌ No se encontró el plugin fuente en ${GLOBAL_PLUGIN_DIR}" >&2
  exit 1
fi

RULE_BYTES=$(wc -c < "${GLOBAL_PLUGIN_DIR}/rules/AGENTS.md" | tr -d ' ')
if [ "${RULE_BYTES}" -ge 24000 ]; then
  echo "❌ Error: rules/AGENTS.md (${RULE_BYTES} bytes) supera el tope de 24,000 bytes de Antigravity." >&2
  exit 1
fi
echo "📏 Tamaño verificado de rules/AGENTS.md: ${RULE_BYTES} bytes (< 24,000 bytes máx.)"

CANDIDATES=()
if [ "$#" -gt 0 ]; then
  for arg in "$@"; do
    CANDIDATES+=("$arg")
  done
else
  for d in "${HOME}/antigravity"/*; do
    [ -e "$d" ] || continue
    [ -L "$d" ] && continue
    [ -d "$d" ] || continue
    CANDIDATES+=("$d")
  done
  if [ -d "${HOME}/FlexRoute" ] && [ ! -L "${HOME}/FlexRoute" ]; then
    CANDIDATES+=("${HOME}/FlexRoute")
  fi
fi

SYNCED_GIT=0
PUSHED_GIT=0
SYNCED_NONGIT=0

for repo in "${CANDIDATES[@]}"; do
  name="$(basename "$repo")"
  target="${repo}/.agents/plugins/raydel-global-rules"
  mkdir -p "${target}/rules" "${target}/scripts"
  cp "${GLOBAL_PLUGIN_DIR}/plugin.json" "${target}/plugin.json"
  cp "${GLOBAL_PLUGIN_DIR}/rules/AGENTS.md" "${target}/rules/AGENTS.md"
  cp "${GLOBAL_PLUGIN_DIR}/README.md" "${target}/README.md"
  cp "${GLOBAL_PLUGIN_DIR}/scripts/install-global.sh" "${target}/scripts/install-global.sh"
  cp "${GLOBAL_PLUGIN_DIR}/scripts/sync-all-repos.sh" "${target}/scripts/sync-all-repos.sh"
  chmod +x "${target}/scripts/"*.sh

  if [ ! -d "${repo}/.git" ]; then
    echo "📁 [SIN-GIT] ${name}: plugin copiado en .agents/plugins/raydel-global-rules"
    SYNCED_NONGIT=$((SYNCED_NONGIT + 1))
    continue
  fi

  # Verificar que el repo tenga al menos un commit en HEAD
  if ! git -C "$repo" rev-parse --verify HEAD >/dev/null 2>&1; then
    echo "⚠️  [GIT-VACÍO] ${name}: copiado sin commit (repo sin HEAD inicial)"
    continue
  fi

  BRANCH="$(git -C "$repo" symbolic-ref --short HEAD 2>/dev/null || echo "")"
  REMOTE_URL="$(git -C "$repo" remote get-url origin 2>/dev/null || echo "")"

  # Si tiene origin y el remoto está estrictamente adelante del HEAD local, traer la punta remota sin tocar worktree
  PARENT_REF="HEAD"
  if [ "${PUSH_REMOTES}" = "1" ] && [ -n "${REMOTE_URL}" ] && [ -n "${BRANCH}" ]; then
    git -C "$repo" fetch -q origin "${BRANCH}" 2>/dev/null || true
    if git -C "$repo" rev-parse --verify "origin/${BRANCH}" >/dev/null 2>&1; then
      LOCAL_SHA="$(git -C "$repo" rev-parse HEAD)"
      REMOTE_SHA="$(git -C "$repo" rev-parse "origin/${BRANCH}")"
      if [ "${LOCAL_SHA}" != "${REMOTE_SHA}" ] && git -C "$repo" merge-base --is-ancestor "${LOCAL_SHA}" "${REMOTE_SHA}"; then
        PARENT_REF="origin/${BRANCH}"
      fi
    fi
  fi

  TMP_IDX="$(mktemp /tmp/agy_rules_idx.XXXXXX)"
  rm -f "${TMP_IDX}"
  GIT_INDEX_FILE="${TMP_IDX}" git -C "$repo" read-tree "${PARENT_REF}"
  GIT_INDEX_FILE="${TMP_IDX}" git -C "$repo" add -f -- .agents/plugins/raydel-global-rules
  NEW_TREE="$(GIT_INDEX_FILE="${TMP_IDX}" git -C "$repo" write-tree)"
  rm -f "${TMP_IDX}" "${TMP_IDX}.lock"

  BASE_TREE="$(git -C "$repo" rev-parse "${PARENT_REF}^{tree}")"
  if [ "${NEW_TREE}" != "${BASE_TREE}" ]; then
    PARENT_SHA="$(git -C "$repo" rev-parse "${PARENT_REF}")"
    NEW_COMMIT="$(git -C "$repo" commit-tree "${NEW_TREE}" -p "${PARENT_SHA}" -m "chore(rules): sync raydel-global-rules plugin for remote & local Antigravity sessions")"
    if [ -n "${BRANCH}" ]; then
      git -C "$repo" update-ref "refs/heads/${BRANCH}" "${NEW_COMMIT}"
    else
      git -C "$repo" update-ref HEAD "${NEW_COMMIT}"
    fi
    git -C "$repo" reset -q HEAD -- .agents/plugins/raydel-global-rules 2>/dev/null || true
    SYNCED_GIT=$((SYNCED_GIT + 1))
    COMMIT_MSG="commit ${NEW_COMMIT:0:7}"
  else
    git -C "$repo" reset -q HEAD -- .agents/plugins/raydel-global-rules 2>/dev/null || true
    COMMIT_MSG="ya al día"
  fi

  if [ "${PUSH_REMOTES}" = "1" ] && [ -n "${REMOTE_URL}" ] && [ -n "${BRANCH}" ]; then
    if git -C "$repo" push -q origin "${BRANCH}" 2>/dev/null; then
      echo "🚀 [GITHUB]  ${name} (${BRANCH}): ${COMMIT_MSG} -> pushed a ${REMOTE_URL}"
      PUSHED_GIT=$((PUSHED_GIT + 1))
    else
      echo "⚠️  [PUSH-PENDIENTE] ${name} (${BRANCH}): ${COMMIT_MSG} (no se pudo hacer push automático a ${REMOTE_URL})"
    fi
  else
    echo "✅ [GIT-LOCAL] ${name} (${BRANCH:-detached}): ${COMMIT_MSG}"
  fi
done

echo "------------------------------------------------------------"
echo "Resumen: ${SYNCED_GIT} repos Git actualizados | ${PUSHED_GIT} repos subidos a GitHub | ${SYNCED_NONGIT} carpetas locales sincronizadas."
