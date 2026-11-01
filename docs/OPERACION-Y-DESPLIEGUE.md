# Operación y preparación de despliegue de HACHE-TRADER

## Estado actual

**SOLO SIMULACIÓN.** El motor de órdenes reales no existe. No depositar fondos ni configurar API keys con permisos de trading.

GitHub es la fuente de verdad. Este documento y el archivo `deployment/systemd/hache-trader-paper.service` son **plantillas**, no un despliegue realizado.

## Validación segura del API

Desde una copia **verificada y exacta** del repositorio:

```bash
python3 -m hache_trader diagnose --market pearlusdt
python3 -m unittest discover -s tests -v
```

`diagnose` consulta solo datos públicos. Un 403, 429, timeout, libro inválido o mercado ausente debe reportarse como falla; no se permite eludir restricciones del exchange, deshabilitar TLS ni reemplazar datos actuales por precios inventados.

Revisar las estadísticas de 24 h, spreads y profundidad con escepticismo: son señales públicas, no pruebas de que exista capacidad de ejecutar una orden real ni evidencia de ganancias.

## Seguridad de la máquina

El VPS de Hache Natación comparte servicios de negocio y minería. El servicio trader debe tener **su propia identidad** e incluir:

- Checkout inmutable de un SHA revisado de GitHub, fuera de directorios de otras aplicaciones.
- Estado propio bajo `/var/lib/hache-trader-paper`, propietario aislado, modo `0700`.
- Sin sudo general, sin acceso a claves del minero y sin conexión a las bases de datos de Hache Natación.
- Sin puertos entrantes ni interfaz pública; únicamente HTTPS saliente a fuentes autorizadas.
- Límites de recursos y parada/reinicio supervisados.
- Logs sin tokens, contraseñas ni datos sensibles.
- Copia de seguridad del ledger SQLite y restauración probada antes de permitir operación continua.
- Verificación independiente del SHA instalado y prueba de rollback.

La plantilla `DynamicUser=yes` impide depender de un nuevo usuario privilegiado. Sin embargo, para instalar un servicio de sistema se necesitan permisos que **no deben ampliarse por iniciativa propia**. La autorización del usuario `deploy-hache` sigue limitada al comando sudo permitido para Hache Natación; no extenderla al trader sin aprobación explícita.

## Telegram independiente

El bot de HACHE-TRADER debe ser diferente del bot de minería.

1. Crear un bot mediante @BotFather y conservar su token **fuera del chat, logs, GitHub y archivos de configuración versionados**.
2. Obtener el ID exacto del bot, `chat_id` y `user_id` del propietario desde una fuente autorizada.
3. Configurar el token como credencial protegida de systemd y enlazarlo a la identidad verificada, sin mostrarlo en procesos ni stdout.
4. Comprobar que `getWebhookInfo` no indica otro consumidor y que solo este servicio consume `getUpdates`.
5. Probar `/estado`, `/pausar`, `/reanudar` (con confirmación) y `/emergencia` usando únicamente datos ficticios.

La plantilla de servicio incluida arranca **sin Telegram**, intencionalmente. Añadir la configuración de credenciales/Telegram en otro PR cuando exista un bot independiente; no insertar un token en el código ni improvisar un ajuste manual en producción.

## Condiciones para encender un simulador permanente

- GitHub PR revisado y todos los checks verdes.
- Pares de mercado verificados en llamadas públicas consecutivas.
- 403/429 y datos ausentes no provocan compra ni reintentos agresivos.
- Persistencia, parada de emergencia, recuperación y autenticación de Telegram verificadas.
- Modo real comprobado como imposible y límites financieros de simulación validados.
- Instalación de servicio y ruta de rollback aprobadas **sin afectar al sitio ni minería**.
- El primer arranque queda pausado y no inicia nuevas entradas hasta un comando autorizado de reanudación.

No considerar rentabilidad demostrada con una muestra de pruebas unitarias. Para evaluar la estrategia, registrar una ventana prospectiva de al menos 14 días y, si se alcanza, una muestra de 100 cierres; documentar tamaño y sesgos de esa muestra. No habilitar fondos reales al cumplir una cifra automáticamente.

## Estado de la conectividad observado

La laptop Hache y el VPS devolvieron respuestas diferentes en el pasado. Es posible obtener 200 en mercados y 403 en comisiones. Cada endpoint debe comprobarse por separado; la ausencia de comisiones verificadas bloquea las entradas.

El VPS no tiene actualmente acceso Git privado por SSH sin corregir la autenticidad del host; el error `Host key verification failed` debe resolverse por el canal administrativo autorizado, **no con** `StrictHostKeyChecking=no` ni ampliando permisos sin autorización.

## Flujo de trabajo

`GitHub -> PR -> checks y revisión -> corrección -> merge -> release por SHA -> despliegue autorizado -> verificación -> rollback si falla`.

No se editará código versionado directamente desde Remote Desktop Commander en el VPS. Este conector solo se utilizará para diagnóstico, despliegue autorizado, logs y verificación.

## Pendientes bloqueantes

- Validar el mercado de referencia y el uso real del endpoint público de comisiones desde el entorno elegido.
- Obtener bot independiente y credenciales privadas sin exponer el token.
- Resolver de forma autorizada el acceso de lectura al repositorio privado desde la infraestructura de despliegue.
- Completar monitoreo del proceso, health checks y credenciales de Telegram.
- Definir procedimiento aprobado de instalación systemd y rollback, sin elevar permisos existentes.
- Auditoría independiente con Work cuando esté disponible; por ahora **PR en borrador y sin merge**.
