# Raydel Global Rules (`raydel-global-rules`)

Plugin oficial de Antigravity con las reglas globales obligatorias para **todos los proyectos** tanto en **local** como en **sesiones remotas** (Antigravity Remote / Cloud Workspaces / contenedores / SSH / CLI `agy` / otra Mac o PC).

## Cómo funciona sin duplicar tokens

1. **En cualquier sesión remota por repositorio (Cloud Workspace / VM / Codespaces / GitHub)**:
   - Cada repositorio lleva `.agents/plugins/raydel-global-rules/`.
   - Al abrir el repositorio en remoto, Antigravity descubre automáticamente `.agents/plugins/raydel-global-rules/rules/AGENTS.md` (Prioridad #1 Workspace) sin configurar nada.
2. **En la Mac local (o cualquier otra máquina propia)**:
   - Está instalado globalmente en `~/.gemini/config/plugins/raydel-global-rules/` (Prioridad #3 Global), cubriendo también carpetas nuevas o sin Git.
   - Como ambos comparten `"name": "raydel-global-rules"`, Antigravity aplica deduplicación por namespace de plugin y carga **una sola instancia** (0 tokens duplicados).

## Comandos rápidos

- **Instalar globalmente en otra máquina / servidor SSH remoto** (desde cualquiera de tus repos clonados):
  ```bash
  bash .agents/plugins/raydel-global-rules/scripts/install-global.sh
  ```
- **Propagar una actualización de las reglas globales a todos tus repositorios locales y GitHub**:
  ```bash
  bash ~/.gemini/config/plugins/raydel-global-rules/scripts/sync-all-repos.sh
  ```
- **Opcional en remoto para Fase 0 Web (`Niveles 6–9`)**:
  - Si deseas conectar una sesión remota al puente Chrome de tu Mac (`:4200` vía túnel HTTPS), define `export HUELLAS_WEB_BRIDGE_URL="https://<tu-tunel>"`.
