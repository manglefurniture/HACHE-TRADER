# Telegram: recuperación ante fallos breves

Solo los errores de transporte `telegram_unavailable` y `telegram_cooldown` tienen una tolerancia de 90 segundos. Durante esa ventana la simulación mantiene el estado autorizado; los controles remotos podrían estar inaccesibles. El propietario puede consultar `/estado` para identificar el estado intermitente.

Si Telegram regresa antes del límite, se borra la incidencia sin exigir nueva reanudación. Si persiste, se bloquean nuevas entradas ficticias y se requiere confirmación manual tras la recuperación. Errores de protocolo, respuestas inválidas y fallos inesperados siguen provocando pausa inmediata. Nunca hay reanudación automática tras una pausa confirmada, manual o de emergencia.

Las posiciones abiertas no se cierran por la pérdida de Telegram. El motor continúa gestionando salidas simuladas mientras SafeTrade y el servidor respondan. Si no hay datos públicos válidos, no puede garantizarse la salida.

Esta revisión no cambia OpenAI, permisos, reglas de riesgo, número de consumidores Telegram ni soporte para operaciones con dinero real. El despliegue se realiza por release de GitHub, con respaldo y rollback; el reinicio requiere nueva confirmación para reanudar la simulación.
