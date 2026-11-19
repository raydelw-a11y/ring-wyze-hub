# Reglas de Proyecto para Claude Code

## Política de Effort (todas las sesiones, incluidas las de la nube)
- **Clasificar al empezar cada tarea**: antes de trabajar, indica en una línea el effort que pide la tarea (`low` / `medium` / `high`):
  - `low`: cambios mecánicos o claros.
  - `medium`: cambios en varios archivos o comportamiento de UI poco claro.
  - `high`: bugs sin causa conocida o que requieren análisis real.
- **Si coincide con el effort de la sesión, continúa.** Si la tarea pide más, **detente antes de hacer el trabajo** y avisa al usuario para que lo suba en el selector (una sesión no puede cambiar su propio effort).
- **Si la tarea pide menos que el effort de la sesión**, avísalo en una línea al empezar ("esta tarea es `low` y la sesión está en `medium`; puedes bajarlo") y continúa sin esperar respuesta.
- **Al lanzar otra sesión para una tarea**, pásale el `effort` explícito según esa clasificación.
- Solo se usan `low`, `medium` y `high`; el valor por defecto es `low`. **Nunca cambies el modelo.**
