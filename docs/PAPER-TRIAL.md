# Prueba de trading automático con dinero ficticio

Estado: **preparado en GitHub, NO desplegado en HACHE Trader**. El bot actual debe continuar en `observe --telegram`, con el radar público y las órdenes bloqueadas.

## Objetivo
Observar resultados simulados, no obtener ganancias reales ni predecir con certeza el mercado. La meta del usuario de ganar 2 USD con 50 USD equivale al 4 % del capital y **no es un rendimiento esperado ni garantizado**.

## Restricciones de la fase

- Se operan exclusivamente saldos ficticios. No existe una vía de transporte de órdenes reales en el adaptador SafeTrade.
- Capital: **50 USDT simulados**. Máximo 25 USDT por entrada. Máximo tres entradas nuevas por día local de Cancún y una sola posición a la vez.
- Pérdida prevista máxima: 0.50 USDT por entrada. Presupuesto diario de pérdida realizada: 1.50 USDT.
- Stop, objetivos, colas de órdenes y ejecuciones son aproximaciones basadas en profundidad pública. Una orden simulada no demuestra una ejecución real.
- Sin apalancamiento, retiros, saldo privado ni API key de trading.
- Los importes netos del ledger incluyen las comisiones públicas **estimadas**, no verificadas contra la cuenta; no representan ganancias retirable o liquidez garantizada.
- Ante errores HTTP, desactualización de mercado, saldo inválido o auditoría inconsistente, **no abrir nuevas entradas**.
- El arranque reconcilia el ledger y deja el trading ficticio **pausado**. Para activar órdenes ficticias desde Telegram se requiere `/reanudar` y una segunda confirmación privada, tras un ciclo saludable.
- `/pausar` impide nuevas entradas. Una posición ficticia abierta permanece registrada hasta ejecutarse una salida simulada o gestionarse como remanente. `/emergencia` no equivale a liquidación garantizada.

## Entrega y despliegue separados

1. PR y pruebas completas en GitHub, con revisión automática y comentarios resueltos.
2. Versionar una plantilla **alternativa** de systemd en `deployment/systemd/hache-trader-telegram-trial.service`. La unidad instalada conserva el nombre `hache-trader-telegram-paper.service` para no iniciar dos consumidores de `getUpdates`.
3. Preparar un release inmutable con SHA y SHA256 comprobados.
4. El archivo `deployment/upgrade-telegram-trial.sh` realiza backup consistente del ledger, detiene el servicio observador, instala la variante trial en su lugar, verifica arranque y restaura la versión anterior si falla. **Requiere root**; no ampliar permisos de `deploy-hache`.
5. Verificar `systemctl is-active`, `/estado`, `/saldo` y `/riesgo` sin pulsar Reanudar inicialmente.
6. Tras comprobar el estado, el propietario podrá autorizar la primera **operación ficticia** mediante confirmación en Telegram. Eso **no** autoriza trading real.

Nunca iniciar a la vez `observe` y `trial` sobre el mismo archivo SQLite ni con el mismo bot de Telegram.

## Evaluación de resultados
- Recopilar datos prospectivos durante al menos 14 días y buscar una muestra significativa de ejecuciones simuladas (100 cierres como objetivo de muestra, no como garantía de que ocurrirán).
- Registrar entradas rechazadas, spreads, comisiones, slippage, pérdidas, tiempo en posición, máximas excursiones adversas y ganancia neta.
- Si no se producen operaciones por filtros o liquidez, reportar **0 operaciones**, nunca inventar beneficios.
- La auditoría final con Work y una revisión separada de permisos de API son requisitos adicionales para siquiera considerar fondos reales. La fase real sigue deshabilitada en código.

## Nota sobre la privacidad
El repositorio es público por elección del propietario. Nunca guardar tokens, ID privados sensibles, credenciales de SafeTrade o datos de cuenta. Las credenciales de Telegram están fuera de GitHub, bajo los permisos restrictivos ya establecidos en el VPS.
