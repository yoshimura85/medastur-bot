"""
Telegram bot for monitoring appointments at paciente.medastur.com.

Commands:
  /start      - Welcome message
  /login      - Set credentials (conversational flow)
  /logout     - Remove stored credentials
  /check      - Manual appointment check
  /monitor    - Start automatic monitoring
  /stop       - Stop automatic monitoring
  /interval   - Set check interval (minutes)
  /status     - Show monitoring status
  /help       - Show help
"""
import asyncio
import logging
import os
from typing import Optional

from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.constants import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from checker import check_appointments_for_user
from storage import (
    delete_credentials,
    get_all_monitoring_users,
    load_credentials,
    load_user_config,
    save_credentials,
    save_user_config,
)

load_dotenv()
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Conversation states
ASK_USERNAME, ASK_PASSWORD = range(2)
ASK_INTERVAL = range(1)

DEFAULT_INTERVAL_MINUTES = 30
MIN_INTERVAL_MINUTES = 5


# ── Scheduler ──────────────────────────────────────────────────────────────────

scheduler = AsyncIOScheduler()
_app_ref: Optional[Application] = None


async def _notify(telegram_id: int, message: str) -> None:
    if _app_ref:
        await _app_ref.bot.send_message(
            chat_id=telegram_id,
            text=message,
            parse_mode=ParseMode.MARKDOWN,
        )


async def _scheduled_check(telegram_id: int) -> None:
    logger.info("Scheduled check for user %d", telegram_id)
    await check_appointments_for_user(telegram_id, _notify)


def _reschedule_user(telegram_id: int, interval_minutes: int) -> None:
    job_id = f"check_{telegram_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    scheduler.add_job(
        _scheduled_check,
        trigger=IntervalTrigger(minutes=interval_minutes),
        id=job_id,
        args=[telegram_id],
        replace_existing=True,
    )
    logger.info("Scheduled job %s every %d min", job_id, interval_minutes)


def _remove_schedule(telegram_id: int) -> None:
    job_id = f"check_{telegram_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _send(update: Update, text: str, **kwargs) -> None:
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, **kwargs)


# ── /start ─────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send(
        update,
        "👋 *Bienvenido al Bot de Citas Medastur*\n\n"
        "Este bot monitoriza las citas disponibles en *paciente.medastur.com* "
        "y te notifica cuando aparezcan nuevas.\n\n"
        "Para empezar usa /login para introducir tus credenciales.\n"
        "Después usa /monitor para activar las comprobaciones automáticas.\n\n"
        "📋 /help para ver todos los comandos.",
    )


# ── /help ──────────────────────────────────────────────────────────────────────

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send(
        update,
        "*Comandos disponibles:*\n\n"
        "/login — Guardar tus credenciales del portal\n"
        "/logout — Eliminar credenciales guardadas\n"
        "/check — Comprobar citas ahora mismo\n"
        "/monitor — Activar comprobaciones automáticas\n"
        "/stop — Desactivar comprobaciones automáticas\n"
        "/interval — Cambiar el intervalo de comprobación\n"
        "/status — Ver el estado del monitoreo\n"
        "/help — Esta ayuda",
    )


# ── /login (conversation) ──────────────────────────────────────────────────────

async def cmd_login_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _send(
        update,
        "🔐 Vamos a guardar tus credenciales.\n\n"
        "Introduce tu *usuario o DNI* del portal Medastur:\n"
        "(escribe /cancelar para abortar)",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ASK_USERNAME


async def login_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["login_username"] = update.message.text.strip()
    await _send(update, "🔑 Ahora introduce tu *contraseña*:")
    return ASK_PASSWORD


async def login_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    username = context.user_data.pop("login_username", "")
    password = update.message.text.strip()
    telegram_id = update.effective_user.id

    # Delete the message with the password for security
    try:
        await update.message.delete()
    except Exception:
        pass

    if not username or not password:
        await _send(update, "❌ Usuario o contraseña vacíos. Usa /login para intentarlo de nuevo.")
        return ConversationHandler.END

    save_credentials(telegram_id, username, password)
    await _send(
        update,
        "✅ Credenciales guardadas correctamente.\n\n"
        "Usa /check para comprobar tus citas ahora,\n"
        "o /monitor para activar las comprobaciones automáticas.",
    )
    return ConversationHandler.END


async def login_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("login_username", None)
    await _send(update, "❌ Login cancelado.")
    return ConversationHandler.END


# ── /logout ────────────────────────────────────────────────────────────────────

async def cmd_logout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tid = update.effective_user.id
    delete_credentials(tid)
    _remove_schedule(tid)
    save_user_config(tid, {"monitoring": False})
    await _send(update, "🗑️ Credenciales eliminadas y monitoreo detenido.")


# ── /check ─────────────────────────────────────────────────────────────────────

async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tid = update.effective_user.id
    if not load_credentials(tid):
        await _send(update, "❌ No hay credenciales. Usa /login primero.")
        return

    msg = await update.message.reply_text("🔍 Comprobando citas... por favor espera.")
    result = await check_appointments_for_user(tid, _notify)
    await msg.edit_text(result, parse_mode=ParseMode.MARKDOWN)


# ── /monitor ───────────────────────────────────────────────────────────────────

async def cmd_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tid = update.effective_user.id
    if not load_credentials(tid):
        await _send(update, "❌ No hay credenciales. Usa /login primero.")
        return

    cfg = load_user_config(tid)
    interval = cfg.get("interval_minutes", DEFAULT_INTERVAL_MINUTES)

    _reschedule_user(tid, interval)
    save_user_config(tid, {"monitoring": True, "interval_minutes": interval})

    await _send(
        update,
        f"✅ Monitoreo activado. Comprobaré las citas cada *{interval} minutos*.\n"
        "Te notificaré si aparecen citas nuevas.\n\n"
        "Usa /stop para desactivarlo o /interval para cambiar la frecuencia.",
    )


# ── /stop ──────────────────────────────────────────────────────────────────────

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tid = update.effective_user.id
    _remove_schedule(tid)
    save_user_config(tid, {"monitoring": False})
    await _send(update, "⏹️ Monitoreo detenido. Usa /monitor para reactivarlo.")


# ── /interval (conversation) ───────────────────────────────────────────────────

async def cmd_interval_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cfg = load_user_config(update.effective_user.id)
    current = cfg.get("interval_minutes", DEFAULT_INTERVAL_MINUTES)
    await _send(
        update,
        f"⏱️ Intervalo actual: *{current} minutos*\n\n"
        f"Introduce el nuevo intervalo en minutos (mínimo {MIN_INTERVAL_MINUTES}):\n"
        "(escribe /cancelar para abortar)",
    )
    return 0  # ASK_INTERVAL state 0


async def interval_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tid = update.effective_user.id
    text = update.message.text.strip()
    try:
        minutes = int(text)
        if minutes < MIN_INTERVAL_MINUTES:
            await _send(update, f"❌ El mínimo es {MIN_INTERVAL_MINUTES} minutos. Inténtalo de nuevo:")
            return 0
    except ValueError:
        await _send(update, "❌ Introduce un número entero de minutos:")
        return 0

    save_user_config(tid, {"interval_minutes": minutes})

    cfg = load_user_config(tid)
    if cfg.get("monitoring"):
        _reschedule_user(tid, minutes)
        await _send(update, f"✅ Intervalo actualizado a *{minutes} minutos* y monitoreo reprogramado.")
    else:
        await _send(update, f"✅ Intervalo guardado: *{minutes} minutos*. Activa el monitoreo con /monitor.")
    return ConversationHandler.END


async def interval_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _send(update, "❌ Cambio de intervalo cancelado.")
    return ConversationHandler.END


# ── /status ────────────────────────────────────────────────────────────────────

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tid = update.effective_user.id
    has_creds = load_credentials(tid) is not None
    cfg = load_user_config(tid)
    monitoring = cfg.get("monitoring", False)
    interval = cfg.get("interval_minutes", DEFAULT_INTERVAL_MINUTES)

    job_active = scheduler.get_job(f"check_{tid}") is not None

    lines = ["*Estado del monitoreo:*\n"]
    lines.append(f"🔐 Credenciales: {'✅ guardadas' if has_creds else '❌ no configuradas'}")
    lines.append(f"📡 Monitoreo: {'✅ activo' if monitoring and job_active else '⏹️ inactivo'}")
    if monitoring:
        lines.append(f"⏱️ Intervalo: cada {interval} minutos")

    await _send(update, "\n".join(lines))


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    global _app_ref

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN not set in .env")

    app = Application.builder().token(token).build()
    _app_ref = app

    cancel_filter = filters.Regex(r"^/cancelar$")

    # Login conversation
    login_conv = ConversationHandler(
        entry_points=[CommandHandler("login", cmd_login_start)],
        states={
            ASK_USERNAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_username),
                CommandHandler("cancelar", login_cancel),
            ],
            ASK_PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_password),
                CommandHandler("cancelar", login_cancel),
            ],
        },
        fallbacks=[CommandHandler("cancelar", login_cancel)],
    )

    # Interval conversation
    interval_conv = ConversationHandler(
        entry_points=[CommandHandler("interval", cmd_interval_start)],
        states={
            0: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, interval_set),
                CommandHandler("cancelar", interval_cancel),
            ],
        },
        fallbacks=[CommandHandler("cancelar", interval_cancel)],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(login_conv)
    app.add_handler(CommandHandler("logout", cmd_logout))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("monitor", cmd_monitor))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(interval_conv)
    app.add_handler(CommandHandler("status", cmd_status))

    # Restore monitoring for users who had it active
    async def on_startup(app: Application) -> None:
        scheduler.start()
        for tid in get_all_monitoring_users():
            cfg = load_user_config(tid)
            interval = cfg.get("interval_minutes", DEFAULT_INTERVAL_MINUTES)
            _reschedule_user(tid, interval)
            logger.info("Restored monitoring for user %d every %d min", tid, interval)

    async def on_shutdown(app: Application) -> None:
        scheduler.shutdown(wait=False)

    app.post_init = on_startup
    app.post_shutdown = on_shutdown

    logger.info("Bot started. Polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
