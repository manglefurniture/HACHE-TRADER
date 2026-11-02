# Aprendizaje controlado — únicamente con operaciones ficticias

La simulación de HACHE Trader necesita datos suficientes para generar resultados medibles. **El agente no entrena ni ajusta automáticamente la estrategia o el modelo de OpenAI**: registra ejemplos, resultados y motivos de rechazo para evaluarlos posteriormente.

## Mejor acceso a datos nativos

Primero se evalúan velas nativas de 1 minuto; si son insuficientes, están desactualizadas, tienen huecos o volumen irregular, se prueban velas públicas de 5 y finalmente 15 minutos de SafeTrade. Si existe información válida pero no hay señal, también se consulta el siguiente intervalo. Cada intervalo utiliza exactamente 20 velas cerradas consecutivas con volumen positivo y sin discontinuidades. No se rellenan minutos, no se inventa actividad, no se transforman velas de otra duración ni se ignoran errores críticos del proveedor.

Los controles de libro de órdenes, comisiones públicas, liquidez, spread, volatilidad, antirrepetición, cierre ficticio de posiciones y riesgo se ejecutan sin excepciones adicionales.

## Prueba exploratoria de estudio

Cuando no aparece una señal ordinaria de tendencia/recuperación en intervalos válidos de 5 o 15 minutos, puede aparecer una señal etiquetada `study_probe` en una recuperación moderada de precio, cerca de sus medias y sin alta volatilidad. Es una hipótesis de estudio, **no una recomendación de compra**. Solo se ejecuta en modo paper y siempre después del plan de riesgo normal.

- Máximo **una entrada de estudio al día local de Cancún**, contando incluso órdenes canceladas o no ejecutadas.
- Importe ficticio de estudio **hasta 10 USDT** y objetivo neto estimado de **0.20 USDT**, sujetos a reglas de mercado, redondeo y comisiones. No existe obligación de usar todo el capital.
- Límites globales SIN CAMBIOS: capital ficticio 50 USDT, hasta 3 intentos de compra diarios, una posición, pérdida máxima por operación 0.50 USDT, pérdida diaria 1.50 USDT, máximo global por orden 25 USDT.
- Los cierres siguen sujetos a stop/objetivo y libro fresco. Si no hay datos o la profundidad no soporta una operación válida, **no se ejecuta**.
- `/diagnostico` registra candidatas y cuándo se alcanza el cupo de una prueba de estudio. `/historial` y las notificaciones de cierres siguen siendo la fuente de resultados efectivamente ejecutados.

Un mayor número de intentos ficticios no implica mayor rentabilidad. No se promete que este cambio produzca ninguna compra; se medirá la tasa de señales, órdenes no ejecutadas, cierres y resultados netos antes de volver a ajustar.

## Seguridad, despliegue y reversión

Solo después del PR, checks, pruebas y revisión automática: desplegar el SHA de GitHub mediante el instalador versionado de siempre. Hacer respaldo previo y conservar rollback. El reinicio del bot pausa entradas nuevas y exige confirmación de `/reanudar`. No modificar código en el VPS. La integración OpenAI shadow y Telegram continúa independiente. El uso de fondos reales permanece deshabilitado.

Tras desplegar, comparar `/estado`, `/diagnostico` y `/historial`. La evaluación de la estrategia exploratoria será posterior y **no autoriza actualizaciones automáticas de política de trading**.
