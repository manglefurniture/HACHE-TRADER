# Diagnóstico seguro de fallos del motor ficticio

Cuando un ciclo de SafeTrade o de ejecución no resulta verificable, el motor **sigue pausando entradas** y no considera cerrada ninguna posición. El nuevo diagnóstico no relaja ninguna condición ni vuelve a reanudar automáticamente el trading ficticio.

## Qué verá el propietario

`/estado` conserva `Error del mercado: market_or_execution_unavailable` y añade:

`Último fallo del motor: DD/MM HH:MM · <fase> / <código>`

Las fases son las etapas fijas del motor (por ejemplo, `public_fees`, `market_catalog`, `public_tickers`, `candidate_scan`, `entry_validation`, `paper_execution`). El código es una clasificación local limitada a una lista segura: por ejemplo, `safetrade_http_429`, `safetrade_transport_or_schema_error`, `circuit_open`, `stale_ticker` o `market_guard_rejected`. Los códigos no incluyen texto de excepción, URL, token ni respuesta de proveedor.

La última falla queda registrada con fecha aun si un análisis posterior se completa y `last_error` vuelve a ser nulo. Los registros previos a este despliegue solo tienen el error genérico: no es posible reconstruir su motivo exacto.

## Mensajes repetidos

Se entrega **como máximo una alerta general por 30 minutos** aunque haya ciclos exitosos intermedios. Cada intento fallido sigue dejando el motor pausado, la marca de fallo en SQLite y el registro auditado; la reducción de alertas no elimina la protección. Los cierres simulados, emergencias, notificaciones Telegram y los controles deterministas no se alteran.

## Verificación

Pruebas offline: fallos de red/proveedor, recuperación de lecturas, una segunda caída dentro de 30 minutos, persistencia entre reaperturas de SQLite, identificación de etapa de mercado y protección ante cadenas de error que intenten incluir secretos.

Despliegue: GitHub, revisión automática y CI, release por SHA, respaldo SQLite/rollback. El instalador reinicia Telegram y deja las nuevas entradas ficticias pausadas hasta confirmar `/reanudar`. Antes del reinicio comprobar `/posiciones`; no desplegar durante una posición ficticia abierta sin evaluar la interrupción temporal de su supervisión. No se habilitan operaciones reales.
