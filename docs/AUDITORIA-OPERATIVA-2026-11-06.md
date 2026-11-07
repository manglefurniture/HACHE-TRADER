# Auditoría operativa — 2026-11-06 (solo lectura)

## Evidencia verificada
- GitHub main y release instalado: `40367ed9da92a72945a9ed380458a4bc17e5711a`.
- `hache-trader-telegram-paper.service`: `trial --telegram`, activo desde 2026-11-03 21:56 UTC, `NRestarts=0`.
- `hache-trader-ai-shadow.timer` habilitado; último servicio terminó con código 0. Esto **no** confirma respuesta válida de OpenAI.
- Suite offline en el checkout desplegado: **152 pruebas, OK**.
- Diagnóstico público de `pearlusdt`: mercados, comisiones, ticker, profundidad y velas accesibles. Spread observado 0.54945 %, superior al máximo de 0.4 %. Una muestra no describe el mercado completo.
- El usuario `gpt-agent` no tiene acceso al SQLite del servicio protegido por `DynamicUser`; el comando `status` desde esa identidad devolvió `configuration_or_storage_unavailable`. **No** es evidencia de que el servicio esté roto.
- Sin verificación todavía de ciclos históricos, causas de descarte, pausas, operaciones o resultados de OpenAI. No se debe afirmar que el bot haya ejecutado entradas.
- Repositorio público: mantener secretos y cualquier dato sensible fuera del código y de los informes.

## Bloqueos arquitectónicos comprobados
1. `Worker.cycle` pone `paused=True` en fallos de mercado/execution generales. Recuperar conectividad no implica reanudar automáticamente.
2. Señales basadas en medias 5/20 y exploración acotada; no hay entrenamiento automático ni adaptación de estrategia.
3. OpenAI shadow es observador separado; no tiene capacidad de órdenes ni alimenta automáticamente la política.
4. Máximo 5 mercados elegibles por ciclo; velas nativas de 1, 5 y 15 minutos deben validarse; profundidad, comisiones y riesgo pueden rechazar entradas.
5. `Policy` fija límites máximos de capital, riesgo, spread y pérdida. No flexibilizar antes de cuantificar descartes.
6. La documentación contiene referencias históricas a etapas no desplegadas; distinguir siempre estado del código de estado del servicio.

## Siguiente medición (sin alterar permisos)
Solicitar al propietario el resultado **privado** de `/estado`, `/diagnostico`, `/historial`, `/posiciones` y `/riesgo` del bot; no incluir tokens, IDs privados ni capturas de conversaciones personales en GitHub. Alternativamente, utilizar un mecanismo de diagnóstico agregado, revisado y desplegado por SHA, que emita únicamente conteos y códigos de error permitidos desde la identidad del servicio.

Verificar en ese orden: pausado/emergencia; último ciclo y fallo; filtros por mercado; señales y candidatos; órdenes enviadas/llenadas; posiciones y cierres; cuota de estudio; ejecución OpenAI con estado real, no solo estado de systemd.

## Plan de cambios, sujeto a evidencia
- Instrumentar métricas agregadas, con retención y privacidad; probar cero fugas y reinicios.
- Separar fallos temporales recuperables de fallos críticos; **nunca** reanudar pausas manuales, emergencias, fallos de integridad o pérdidas de supervisión de posiciones.
- Comparar estrategias en simulación con muestras prospectivas y pruebas fuera de muestra; no permitir que la IA altere límites duros ni mande órdenes.
- Mantener operaciones reales imposibles hasta una autorización y revisión independientes.

## Puerta de despliegue
GitHub -> rama/PR -> tests/CI/revisiones -> merge -> release por SHA -> respaldo SQLite -> despliegue autorizado -> verificar Telegram y estado del ledger. No reiniciar un servicio con posición abierta sin plan de continuidad y reversión. No editar código en el VPS. Este documento **no autoriza** trading real ni cambia el comportamiento del agente.
