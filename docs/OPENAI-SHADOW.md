# HACHE Trader — OpenAI shadow (fase 1)

El observador **no forma parte del motor**, no importa `Worker`, `Store`, `PaperEngine` ni `Channel`, no consulta el ledger ni consume Telegram. Solo usa datos públicos de SafeTrade, y no tiene ruta para operar. El servicio es separado y no cambia los límites de las operaciones ficticias.

- Modelo fijo `gpt-5.4-nano`, Responses API, `store: false`, JSON schema y `max_output_tokens=450`.
- Datos enviados: símbolos USDT, volumen público de 24 h, apertura/último, 5 niveles de profundidad, bid/ask, spread, tendencia y volatilidad de velas cerradas validadas y sello UTC. Nunca claves, balances ni datos privados.
- Los indicadores no verificables quedan `null`. `interesting`, `caution` y `avoid` son clasificaciones orientativas y **no habilitan compras**.
- Base persistente propia: `/var/lib/hache-trader-ai-shadow/ai-shadow.sqlite`, con observaciones públicas, respuesta, estados, timestamps y tokens por solicitud. Cada llamada se reserva antes del HTTP; un reinicio no repite el intento dentro de su bloque de 6 horas.
- Cuotas locales: 4 intentos al día UTC, 100 al mes, 4000 y 80000 tokens respectivamente. Reservar 1000 tokens conservadores por intento sin medición. **Las cuotas no equivalen a un control exacto en dólares:** mantener límite del proyecto de 2 USD en OpenAI.
- Timer cuatro veces al día UTC con hasta 3 minutos de retardo; 15 s de timeout OpenAI. SafeTrade/OpenAI inaccesibles no afectan al motor, quedan como estado de fallo.

## Despliegue con revisión y rollback

No desplegar antes de que PR/CI/revisiones estén aprobados. Usar release exacto por SHA, con el flujo existente `deployment/upgrade-telegram-trial.sh` (respaldo SQLite y rollback), y comprobar que el bot sigue siendo el único consumidor Telegram. El upgrade del motor reinicia el servicio en pausa; coordinar antes.

Después del despliegue del SHA, como root autorizado y **sin leer ni copiar el secreto**:

```bash
stat -c '%u:%a' /etc/hache-trader/openai_api_key # Debe ser 0:600
install -m 0644 /opt/hache-trader/current/deployment/systemd/hache-trader-ai-shadow.service /etc/systemd/system/hache-trader-ai-shadow.service
install -m 0644 /opt/hache-trader/current/deployment/systemd/hache-trader-ai-shadow.timer /etc/systemd/system/hache-trader-ai-shadow.timer
systemd-analyze verify /etc/systemd/system/hache-trader-ai-shadow.service /etc/systemd/system/hache-trader-ai-shadow.timer
systemctl daemon-reload
systemctl enable --now hache-trader-ai-shadow.timer
systemctl start hache-trader-ai-shadow.service
systemctl show -p Result hache-trader-ai-shadow.service
journalctl -u hache-trader-ai-shadow.service --no-pager -n 10
```

Verificar en la SQLite independiente una fila `completed`, totales de tokens reales y snapshot comprobable; no se debe declarar OpenAI operativo sin esa prueba. Rollback del analista: `systemctl disable --now hache-trader-ai-shadow.timer; systemctl stop hache-trader-ai-shadow.service`. Conservar la BD para auditoría. Un rollback completo del release utiliza el procedimiento versionado del servicio existente.

**Fase 2, no incluida:** acceso a snapshots desde el único proceso Telegram existente, limitado al propietario, sin segundo `getUpdates`; respuestas naturales usando estado local comprobable, con colas para no bloquear el loop.
