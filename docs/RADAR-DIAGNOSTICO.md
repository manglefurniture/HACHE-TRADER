# Diagnóstico de oportunidades ficticias del radar

En `@HacheTraderBot`, usar **/diagnostico** (o el botón Diagnóstico), exclusivamente en el chat privado autorizado y en modo de simulación.

El reporte responde, sin acceder a la API de OpenAI:

- Número de ciclos completados en el día local de Cancún.
- Número de evaluaciones de mercados elegibles que efectivamente llegaron al análisis de velas/profundidad; **no son mercados únicos**, porque el radar repite y rota símbolos.
- Evaluaciones donde apareció al menos una señal, y evaluaciones que además superaron los filtros de entrada.
- Principales motivos de descarte, separando expresamente volumen insuficiente, velas inválidas, ausencia de señal, spread, profundidad, volatilidad y riesgo.
- Última razón de no abrir una compra: sin candidatos, simulación pausada, posición ya abierta, límite diario, emergencia, parada externa, entrada previamente evaluada o compra ficticia sin ejecución.

El radar continúa revisando un máximo de 5 mercados elegibles por ciclo. Los mercados con estado «pendiente de análisis» **no cuentan como rechazados**. Ningún dato anterior al despliegue se reconstruye ni se inventa.

## Seguridad y persistencia

El historial se guarda de forma adicional en `scan_diagnostics` dentro del SQLite existente de simulación y su respaldo versionado. Retención de 7 días; la consulta del bot resume únicamente el día corriente de Cancún. Los motivos se guardan como códigos definidos localmente, no como texto arbitrario de errores de proveedores. Los fallos de escritura del diagnóstico no alteran posiciones ni pausas del motor.

No hay cambios a la estrategia, límites de riesgo, fondos ficticios, conexión Telegram o OpenAI, ni posibilidad de trading real. El diagnóstico no ejecuta órdenes.

## Validación

En pruebas offline, verificar la clasificación de motivos, la distinción de señales frente a candidatos, entradas ejecutadas y pausadas, los reinicios con datos persistidos, la privacidad y que fallos del diagnóstico no interrumpen la simulación.

Desplegar desde GitHub por SHA exacto tras CI y revisión automática. El procedimiento existente de despliegue hace respaldo SQLite y reinicia el servicio Telegram dejando las entradas ficticias **pausadas hasta /reanudar con confirmación**. No modificar archivos versionados en el VPS.
