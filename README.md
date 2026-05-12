# medastur-bot

Bot de Telegram que monitoriza las citas disponibles en [paciente.medastur.com](https://paciente.medastur.com/Inicio.aspx) y te notifica automáticamente cuando aparecen nuevas.

## Características

- 🔐 Login seguro: las credenciales se guardan cifradas localmente
- 📅 Comprobación manual o automática de citas disponibles
- 🔔 Notificaciones inmediatas cuando aparecen citas nuevas
- ⏱️ Intervalo de comprobación configurable (mínimo 5 minutos)
- 🔄 Restauración automática del monitoreo al reiniciar el bot

## Requisitos

- Python 3.11+
- Un bot de Telegram (crear con [@BotFather](https://t.me/botfather))

## Instalación

```bash
git clone https://github.com/TU_USUARIO/medastur-bot.git
cd medastur-bot

python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/Mac:
source .venv/bin/activate

pip install -r requirements.txt
```

## Configuración

```bash
cp .env.example .env
```

Edita `.env` y añade tu token de Telegram:

```
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrSTUvwxYZ
```

> La `ENCRYPTION_KEY` se genera automáticamente la primera vez que ejecutas el bot.

## Uso

```bash
python bot.py
```

### Comandos del bot

| Comando | Descripción |
|---------|-------------|
| `/start` | Mensaje de bienvenida |
| `/login` | Introducir credenciales del portal (conversación guiada) |
| `/logout` | Eliminar credenciales guardadas |
| `/check` | Comprobar citas ahora mismo |
| `/monitor` | Activar comprobaciones automáticas |
| `/stop` | Detener comprobaciones automáticas |
| `/interval` | Cambiar el intervalo de comprobación |
| `/status` | Ver estado del monitoreo |
| `/help` | Ayuda |

### Flujo de uso típico

1. Envía `/login` al bot
2. Introduce tu usuario/DNI cuando te lo pida
3. Introduce tu contraseña (el mensaje se borra automáticamente)
4. Usa `/check` para verificar que funciona
5. Usa `/monitor` para activar las alertas automáticas

## Seguridad

- Las contraseñas se cifran con **Fernet (AES-128)** antes de guardarse
- Los mensajes con contraseñas se eliminan automáticamente del chat
- La carpeta `data/` y el archivo `.env` están en `.gitignore`

## Ejecución continua (servidor)

Para ejecutar el bot 24/7 puedes usar `systemd`, `supervisor`, o Docker:

```bash
# Con nohup (simple)
nohup python bot.py &

# Ver logs
tail -f nohup.out
```

## Estructura del proyecto

```
medastur-bot/
├── bot.py          # Bot de Telegram y manejadores de comandos
├── scraper.py      # Scraping del portal ASP.NET de Medastur
├── checker.py      # Lógica de comparación y notificación
├── storage.py      # Almacenamiento cifrado de credenciales
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```
