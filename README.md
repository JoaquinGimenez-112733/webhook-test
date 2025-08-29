# Webhook HacknPlan → Discord

Este proyecto permite **enlazar HacknPlan con Discord** mediante un webhook, para recibir notificaciones de eventos directamente en un canal.

## Requisitos

- Cuenta en [Render](https://render.com/) (probado en el plan gratuito).
- Un canal de Discord con permisos para crear **webhooks**.
- Acceso a la configuración de **webhooks** en HacknPlan.

## Configuración

### 1. Discord
1. En el canal de Discord donde quieras recibir las notificaciones, crea un **Webhook**.
2. Copia la URL que te proporciona Discord.
3. Define una variable de entorno en tu servicio llamada: DISCORD_WEBHOOK_URL

### 2. Token
- Genera un token propio (string alfanumérico).
- Guárdalo como variable de entorno en tu servicio: TOKEN

  - Este token se pasará luego como parámetro en la URL de tu servicio para mayor seguridad.

### 3. HacknPlan
1. En HacknPlan, dirígete a la sección de **Webhooks**.
2. Crea un nuevo webhook y coloca la URL de tu servicio con el siguiente formato: https://URL_DE_SERVICIO.onrender.com/hacknplan?token=TOKEN

`URL_DE_SERVICIO`: la URL que Render (u otro hosting) genere para tu servicio.  
- `TOKEN`: el mismo que configuraste en la variable de entorno.
3. Selecciona los eventos que quieras notificar.  
- Actualmente está probado con **Tasks** e **Items del Design Model**.  
- Es posible que para otros tipos de eventos se requiera desarrollo adicional.






