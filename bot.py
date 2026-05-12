"""
Bot de Telegram para monitorizar huecos en paciente.medastur.com/autoservicio.aspx

Comandos:
  /start      Bienvenida
  /login      Guardar credenciales (conversación)
  /logout     Borrar credenciales
  /filtros    Configurar filtros de búsqueda (conversación)
  /check      Buscar huecos ahora
  /monitor    Activar monitoreo automático
  /stop       Detener monitoreo
  /interval   Cambiar intervalo (minutos)
  /status     Estado actual
  /help       Ayuda
"""
import asyncio
import logging
import os
from typing import Optional

from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters,
)
from telegram.constants import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from checker import check_for_user
from scraper import COMPANIAS, ESPECIALIDADES_DEFAULT, HORAS
from storage import (
    delete_credentials, get_all_monitoring_users,
    load_credentials, load_user_config,
    save_credentials, save_user_config,
)

load_dotenv()
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Conversation states ───────────────────────────────────────────────────────
(ASK_USER, ASK_PASS) = range(2)
(F_COMPANIA, F_ESPECIALIDAD, F_MEDICO, F_HORA_DESDE, F_HORA_HASTA) = range(5)
(ASK_INTERVAL,) = range(1)

DEFAULT_INTERVAL = 30
MIN_INTERVAL = 5

scheduler = AsyncIOScheduler()
_app: Optional[Application] = None


# ── Scheduler helpers ─────────────────────────────────────────────────────────

async def _notify(tid: int, msg: str) -> None:
    if _app:
        await _app.bot.send_message(chat_id=tid, text=msg, parse_mode=ParseMode.MARKDOWN)


async def _scheduled_check(tid: int) -> None:
    logger.info("Scheduled check for user %d", tid)
    await check_for_user(tid, _notify)


def _reschedule(tid: int, minutes: int) -> None:
    jid = f"check_{tid}"
    if scheduler.get_job(jid):
        scheduler.remove_job(jid)
    scheduler.add_job(_scheduled_check, IntervalTrigger(minutes=minutes),
                      id=jid, args=[tid], replace_existing=True)


def _remove_schedule(tid: int) -> None:
    jid = f"check_{tid}"
    if scheduler.get_job(jid):
        scheduler.remove_job(jid)


# ── Utility ───────────────────────────────────────────────────────────────────

async def _reply(update: Update, text: str, keyboard=None, **kw) -> None:
    markup = (ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
              if keyboard else ReplyKeyboardRemove())
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=markup, **kw)


def _option_keyboard(options: list[str], cols: int = 2) -> list[list[str]]:
    rows = []
    for i in range(0, len(options), cols):
        rows.append(options[i:i + cols])
    return rows


# ── /start & /help ────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update,
        "👋 *Bot de Citas Medastur*\n\n"
        "Monitoriza huecos libres en *paciente.medastur.com* y te avisa "
        "cuando aparezca alguno.\n\n"
        "1️⃣ /login — introduce tus credenciales\n"
        "2️⃣ /filtros — elige especialidad, médico y horario\n"
        "3️⃣ /monitor — activa las alertas automáticas\n\n"
        "/help para ver todos los comandos.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update,
        "*Comandos disponibles:*\n\n"
        "/login — Guardar credenciales del portal\n"
        "/logout — Borrar credenciales\n"
        "/filtros — Configurar qué buscar (especialidad, médico, horario)\n"
        "/check — Buscar huecos ahora mismo\n"
        "/monitor — Activar comprobaciones automáticas\n"
        "/stop — Detener comprobaciones\n"
        "/interval — Cambiar intervalo (mínimo 5 min)\n"
        "/status — Ver configuración actual\n"
        "/help — Esta ayuda")


# ── /login ────────────────────────────────────────────────────────────────────

async def login_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _reply(update,
        "🔐 Introduce tu *usuario o DNI* del portal Medastur:\n"
        "_(escribe /cancelar para abortar)_")
    return ASK_USER


async def login_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["_login_user"] = update.message.text.strip()
    await _reply(update, "🔑 Ahora introduce tu *contraseña*:")
    return ASK_PASS


async def login_pass(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tid = update.effective_user.id
    username = context.user_data.pop("_login_user", "")
    password = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    if not username or not password:
        await _reply(update, "❌ Datos vacíos. Usa /login para intentarlo de nuevo.")
        return ConversationHandler.END
    save_credentials(tid, username, password)
    await _reply(update,
        "✅ Credenciales guardadas.\n\n"
        "Usa /filtros para configurar qué especialidad buscar,\n"
        "o /check para probar ahora mismo.")
    return ConversationHandler.END


async def conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _reply(update, "❌ Cancelado.")
    return ConversationHandler.END


# ── /logout ───────────────────────────────────────────────────────────────────

async def cmd_logout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tid = update.effective_user.id
    delete_credentials(tid)
    _remove_schedule(tid)
    save_user_config(tid, {"monitoring": False})
    await _reply(update, "🗑️ Credenciales eliminadas y monitoreo detenido.")


# ── /filtros ──────────────────────────────────────────────────────────────────
# States: F_COMPANIA → F_ESPECIALIDAD → F_MEDICO → F_HORA_DESDE → F_HORA_HASTA

def _compania_keyboard() -> list[list[str]]:
    labels = [f"{v}" for k, v in COMPANIAS.items() if k]
    return _option_keyboard(labels, cols=1)


def _especialidad_keyboard() -> list[list[str]]:
    labels = [v for k, v in ESPECIALIDADES_DEFAULT.items() if k]
    return _option_keyboard(labels, cols=1)


def _hora_keyboard() -> list[list[str]]:
    return _option_keyboard(HORAS, cols=4)


async def filtros_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cfg = load_user_config(update.effective_user.id)
    f = cfg.get("filters", {})
    current = (
        f"Compañía: *{COMPANIAS.get(f.get('compania',''), 'cualquiera')}*\n"
        f"Especialidad: *{ESPECIALIDADES_DEFAULT.get(f.get('especialidad',''), f.get('especialidad','cualquiera'))}*\n"
        f"Médico: *{f.get('medico_nombre','cualquiera')}*\n"
        f"Hora desde: *{f.get('hora_desde','08:00')}*  hasta: *{f.get('hora_hasta','20:30')}*"
    )
    await _reply(update,
        f"⚙️ *Configuración actual de filtros:*\n{current}\n\n"
        "Elige la *compañía* aseguradora:\n_(escribe /cancelar para salir)_",
        keyboard=_compania_keyboard())
    context.user_data["_filters"] = dict(f)
    return F_COMPANIA


async def filtros_compania(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    # Resolve label → value
    compania_val = ""
    for k, v in COMPANIAS.items():
        if v.upper() == text.upper() or k == text:
            compania_val = k
            break
    context.user_data["_filters"]["compania"] = compania_val
    context.user_data["_filters"]["compania_nombre"] = COMPANIAS.get(compania_val, text)

    await _reply(update,
        "Elige la *especialidad* médica:\n_(escribe /cancelar para salir)_",
        keyboard=_especialidad_keyboard())
    return F_ESPECIALIDAD


async def filtros_especialidad(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    esp_val = ""
    for k, v in ESPECIALIDADES_DEFAULT.items():
        if v.upper() == text.upper() or k.strip() == text.strip():
            esp_val = k
            break
    context.user_data["_filters"]["especialidad"] = esp_val
    context.user_data["_filters"]["especialidad_nombre"] = ESPECIALIDADES_DEFAULT.get(esp_val, text)

    await _reply(update,
        "¿Quieres filtrar por un *médico concreto*?\n"
        "Escribe su nombre o envía *cualquiera* para no filtrar:",
        keyboard=[["cualquiera"]])
    return F_MEDICO


async def filtros_medico(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text.lower() in ("cualquiera", "-", ""):
        context.user_data["_filters"]["medico"] = ""
        context.user_data["_filters"]["medico_nombre"] = "cualquiera"
    else:
        context.user_data["_filters"]["medico"] = text
        context.user_data["_filters"]["medico_nombre"] = text

    await _reply(update,
        "¿A partir de qué *hora* quieres la cita? (hora de inicio)",
        keyboard=_hora_keyboard())
    return F_HORA_DESDE


async def filtros_hora_desde(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    hora = update.message.text.strip()
    if hora not in HORAS:
        await _reply(update, "❌ Hora no válida. Elige una de la lista:",
                     keyboard=_hora_keyboard())
        return F_HORA_DESDE
    context.user_data["_filters"]["hora_desde"] = hora

    await _reply(update,
        f"¿Hasta qué *hora* (hora límite)?",
        keyboard=_hora_keyboard())
    return F_HORA_HASTA


async def filtros_hora_hasta(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tid = update.effective_user.id
    hora = update.message.text.strip()
    if hora not in HORAS:
        await _reply(update, "❌ Hora no válida. Elige una de la lista:",
                     keyboard=_hora_keyboard())
        return F_HORA_HASTA

    f = context.user_data.pop("_filters", {})
    f["hora_hasta"] = hora
    save_user_config(tid, {"filters": f})

    esp = f.get("especialidad_nombre", "cualquiera")
    med = f.get("medico_nombre", "cualquiera")
    cia = f.get("compania_nombre", "cualquiera")
    await _reply(update,
        f"✅ *Filtros guardados:*\n\n"
        f"🏥 Compañía: *{cia}*\n"
        f"🩺 Especialidad: *{esp}*\n"
        f"👨‍⚕️ Médico: *{med}*\n"
        f"🕐 Horario: *{f.get('hora_desde','08:00')} – {hora}*\n\n"
        "Usa /check para probar o /monitor para activar alertas automáticas.")
    return ConversationHandler.END


# ── /check ────────────────────────────────────────────────────────────────────

async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tid = update.effective_user.id
    if not load_credentials(tid):
        await _reply(update, "❌ No hay credenciales. Usa /login primero.")
        return
    msg = await update.message.reply_text("🔍 Buscando huecos disponibles...")
    result = await check_for_user(tid, _notify)
    await msg.edit_text(result, parse_mode=ParseMode.MARKDOWN)


# ── /monitor & /stop ──────────────────────────────────────────────────────────

async def cmd_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tid = update.effective_user.id
    if not load_credentials(tid):
        await _reply(update, "❌ No hay credenciales. Usa /login primero.")
        return
    cfg = load_user_config(tid)
    minutes = cfg.get("interval_minutes", DEFAULT_INTERVAL)
    _reschedule(tid, minutes)
    save_user_config(tid, {"monitoring": True, "interval_minutes": minutes})
    await _reply(update,
        f"✅ Monitoreo activado. Compruebo cada *{minutes} minutos*.\n"
        "Te notificaré si aparece algún hueco nuevo.\n\n"
        "Usa /stop para pausarlo · /interval para cambiar la frecuencia.")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tid = update.effective_user.id
    _remove_schedule(tid)
    save_user_config(tid, {"monitoring": False})
    await _reply(update, "⏹️ Monitoreo detenido. Usa /monitor para reactivarlo.")


# ── /interval ─────────────────────────────────────────────────────────────────

async def interval_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cfg = load_user_config(update.effective_user.id)
    cur = cfg.get("interval_minutes", DEFAULT_INTERVAL)
    await _reply(update,
        f"⏱️ Intervalo actual: *{cur} minutos*\n\n"
        f"Escribe el nuevo intervalo en minutos (mínimo {MIN_INTERVAL}):\n"
        "_(escribe /cancelar para salir)_")
    return 0


async def interval_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tid = update.effective_user.id
    try:
        minutes = int(update.message.text.strip())
        if minutes < MIN_INTERVAL:
            await _reply(update, f"❌ Mínimo {MIN_INTERVAL} minutos:")
            return 0
    except ValueError:
        await _reply(update, "❌ Escribe un número entero:")
        return 0

    save_user_config(tid, {"interval_minutes": minutes})
    cfg = load_user_config(tid)
    if cfg.get("monitoring"):
        _reschedule(tid, minutes)
        await _reply(update, f"✅ Intervalo actualizado a *{minutes} minutos* y monitoreo reprogramado.")
    else:
        await _reply(update, f"✅ Intervalo guardado: *{minutes} minutos*. Activa el monitoreo con /monitor.")
    return ConversationHandler.END


# ── /status ───────────────────────────────────────────────────────────────────

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tid = update.effective_user.id
    has_creds = load_credentials(tid) is not None
    cfg = load_user_config(tid)
    monitoring = cfg.get("monitoring", False)
    minutes = cfg.get("interval_minutes", DEFAULT_INTERVAL)
    active = scheduler.get_job(f"check_{tid}") is not None
    f = cfg.get("filters", {})

    lines = ["*Estado del monitoreo:*\n"]
    lines.append(f"🔐 Credenciales: {'✅ guardadas' if has_creds else '❌ no configuradas'}")
    lines.append(f"📡 Monitoreo: {'✅ activo' if monitoring and active else '⏹️ inactivo'}")
    lines.append(f"⏱️ Intervalo: cada *{minutes} minutos*")
    lines.append("")
    lines.append("*Filtros de búsqueda:*")
    lines.append(f"🏥 Compañía: *{f.get('compania_nombre','cualquiera')}*")
    lines.append(f"🩺 Especialidad: *{f.get('especialidad_nombre','cualquiera')}*")
    lines.append(f"👨‍⚕️ Médico: *{f.get('medico_nombre','cualquiera')}*")
    lines.append(f"🕐 Horario: *{f.get('hora_desde','08:00')} – {f.get('hora_hasta','20:30')}*")

    await _reply(update, "\n".join(lines))


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    global _app

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN not set in .env")

    app = Application.builder().token(token).build()
    _app = app

    cancel_cmd = CommandHandler("cancelar", conv_cancel)

    login_conv = ConversationHandler(
        entry_points=[CommandHandler("login", login_start)],
        states={
            ASK_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_user), cancel_cmd],
            ASK_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_pass), cancel_cmd],
        },
        fallbacks=[cancel_cmd],
    )

    filtros_conv = ConversationHandler(
        entry_points=[CommandHandler("filtros", filtros_start)],
        states={
            F_COMPANIA:    [MessageHandler(filters.TEXT & ~filters.COMMAND, filtros_compania), cancel_cmd],
            F_ESPECIALIDAD:[MessageHandler(filters.TEXT & ~filters.COMMAND, filtros_especialidad), cancel_cmd],
            F_MEDICO:      [MessageHandler(filters.TEXT & ~filters.COMMAND, filtros_medico), cancel_cmd],
            F_HORA_DESDE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, filtros_hora_desde), cancel_cmd],
            F_HORA_HASTA:  [MessageHandler(filters.TEXT & ~filters.COMMAND, filtros_hora_hasta), cancel_cmd],
        },
        fallbacks=[cancel_cmd],
    )

    interval_conv = ConversationHandler(
        entry_points=[CommandHandler("interval", interval_start)],
        states={
            0: [MessageHandler(filters.TEXT & ~filters.COMMAND, interval_set), cancel_cmd],
        },
        fallbacks=[cancel_cmd],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(login_conv)
    app.add_handler(CommandHandler("logout", cmd_logout))
    app.add_handler(filtros_conv)
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("monitor", cmd_monitor))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(interval_conv)
    app.add_handler(CommandHandler("status", cmd_status))

    async def on_startup(app: Application) -> None:
        scheduler.start()
        for tid in get_all_monitoring_users():
            cfg = load_user_config(tid)
            _reschedule(tid, cfg.get("interval_minutes", DEFAULT_INTERVAL))

    async def on_shutdown(app: Application) -> None:
        scheduler.shutdown(wait=False)

    app.post_init = on_startup
    app.post_shutdown = on_shutdown

    logger.info("Bot started. Polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    asyncio.set_event_loop(asyncio.new_event_loop())
    main()
