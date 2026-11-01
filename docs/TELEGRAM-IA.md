# Fase 2 — conversación explicativa por Telegram

El único consumidor de Telegram continúa siendo `hache-trader-telegram-paper.service`. El motor ficticio sigue en su hilo existente. `telegram_ai.AIAnswerLoop` es **solo un hilo de respuesta**, no un segundo bot ni un motor con permisos de trading.

## Uso por el propietario

El botón `IA` o `/ia` muestra ejemplos. El propietario puede escribir en texto normal preguntas como:

- ¿Por qué no has comprado nada?
- ¿Qué mercados estás analizando?
- ¿Hay alguna oportunidad interesante?
- ¿Por qué descartaste Bitcoin?
- ¿Cuánto hemos ganado hoy?
- ¿Qué errores cometimos?
- ¿Qué harías diferente?
- ¿Qué está impidiendo operar?

La clasificación de intención es local y limitada. Si no reconoce la pregunta, solicita reformularla. No interpreta pedidos de comprar, vender, transferir, cambiar reglas, pausar ni reanudar por lenguaje natural. Los botones de confirmación y los comandos existentes se mantienen deterministas.

El bot comprueba el usuario exacto, chat exacto y tipo de conversación privada **antes** de encolar. Ignora mensajes reenviados, desactualizados y duplicados. El texto original del chat y los identificadores Telegram **no se envían a OpenAI**. Solamente se envía una categoría fija con estados públicos del radar y cifras agregadas de la simulación. No se envían tokens, órdenes individuales, conversaciones de Sharky, datos de clientes ni información real de inversiones.

## Persistencia, fallo seguro y costos

- Una tabla nueva `ai_questions` dentro del ledger existente guarda ID de actualización local, categoría, estado y tokens, pero no texto libre. No altera la cadena de eventos financieros.
- El hilo crea su propia conexión SQLite. Las llamadas de OpenAI se realizan fuera de las transacciones.
- Ante caída de OpenAI, el usuario recibe una indicación; **no se pausa ni modifica el motor**. Si el proceso cae tras comenzar el POST, la consulta se marca `interrupted` y no se repite automáticamente (evita doble gasto).
- `gpt-5.4-nano`, Responses API, `store:false`, JSON estructurado, `max_output_tokens=220`, timeout 12 s.
- Local: 6 preguntas por día UTC, 60 al mes, 6000/50000 tokens diarios/mensuales, 45 segundos entre consultas, máximo 2 pendientes. Mantener también el límite del proyecto OpenAI de 2 USD.
- El último barrido de mercados se considera vigente durante 180 segundos; si es anterior, **no** se envían señales antiguas como si fuesen actuales.
- Las preguntas y respuestas no ejecutan código, herramientas ni mutaciones de posiciones o políticas. **Trading real permanece imposible**.

## Despliegue versionado

No aplicar sin checks y revisión automáticos aprobados. Después del merge, preparar `git archive` desde el SHA verificado en GitHub; ejecutar pruebas y verificar hash antes de usar `deployment/upgrade-telegram-trial.sh`. Requiere root autorizado en ConnectBot; no modificar código versionado directamente en el VPS.

La unidad `deployment/systemd/hache-trader-telegram-trial.service` añade exclusivamente `LoadCredential=openai_api_key:/etc/hache-trader/openai_api_key`, sin modificar el secreto existente ni añadir privilegios. El instalador comprueba que dicho archivo existe y es root:0600 **antes de detener el bot**. Mantener el analista shadow y su temporizador independientes.

El instalador hace backup SQLite consistente, reemplazo por SHA, arranque comprobado y rollback. **Siempre reinicia las entradas ficticias pausadas**, por lo que el propietario deberá ejecutar `/reanudar` y confirmar tras cada actualización. Comprobar unidad de Telegram, timer shadow, consumidor único, `/estado`, `/ia` y una pregunta de prueba. No ejecutar pruebas OpenAI en bucle por el costo: registrar tokens de una única llamada.

**Limitación actual:** el reporte estructurado del servicio shadow está en un directorio privado diferente (`DynamicUser`). No compartir esa SQLite mediante permisos de lectura global; la conversación usa datos verificables del radar del propio simulador. Una mejora posterior podrá crear un protocolo de intercambio de datos públicos versionado y limitado, sin permisos extra.
