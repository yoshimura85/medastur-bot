"""
Bot de Telegram — Medastur appointment monitor

Setup:
  /login    → credenciales del portal
  /filtros  → compañía + especialidad
  /monitor  → activa alertas (cada 60 min por defecto)

Cuando el bot detecta que algún doctor tiene un hueco más temprano
que en la última comprobación, manda una notificación.

Otros comandos:
  /check     Buscar ahora mismo
  /stop      Pausar monitoreo
  /interval  Cambiar frecuencia
  /status    Ver configuración
  /logout    Borrar credenciales
  /help      Ayuda
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
from scraper import COMPANIAS, ESPECIALIDADES
from storage import (
    clear_earliest, delete_credentials, get_all_monitoring_users,
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
(F_COMPANIA, F_ESPECIALIDAD) = range(2)

DEFAULT_INTERVAL = 60   # minutes
MIN_INTERVAL     = 5

scheduler = AsyncIOScheduler()
_app: Optional[Application] = None


# ── Scheduler helpers ─────────────────────────────────────────────────────────

async def _notify(tid: int, msg: str) -> None:
    if _app:
        await _app.bot.send_message(chat_id=tid, text=msg, parse_mode=ParseMode.MARKDOWN)


async def _run_check(tid: int) -> None:
    logger.info("Scheduled check for user %d", tid)
    await check_for_user(tid, _notify)


def _reschedule(tid: int, minutes: int) -> None:
    jid = f"chk_{tid}"
    if scheduler.get_job(jid):
        scheduler.remove_job(jid)
    scheduler.add_job(_run_check, IntervalTrigger(minutes=minutes),
                      id=jid, args=[tid], replace_existing=True)
    logger.info("Scheduled %s every %d min", jid, minutes)


def _unschedule(tid: int) -> None:
    jid = f"chk_{tid}"
    if scheduler.get_job(jid):
        scheduler.remove_job(jid)


# ── UI helpers ────────────────────────────────────────────────────────────────

async def _reply(update: Update, text: str, keyboard: list | None = None) -> None:
    markup = (ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
              if keyboard else ReplyKeyboardRemove())
    await update.message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup
    )


def _kbd(items: list[str], cols: int = 1) -> list[list[str]]:
    return [items[i:i + cols] for i in range(0, len(items), cols)]


# ── /start & /help ─────────────────────────────────────────────────────────────

async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update,
        "👋 *Bot de Citas Medastur*\n\n"
        "Monitoriza huecos libres y te avisa cuando algún médico "
        "tenga disponibilidad *más temprana* que antes.\n\n"
        "Pasos:\n"
        "1️⃣ /login — credenciales del portal\n"
        "2️⃣ /filtros — elige compañía y especialidad\n"
        "3️⃣ /monitor — activa las alertas\n\n"
        "/help para ver todos los comandos.")


async def cmd_help(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update,
        "*Comandos disponibles:*\n\n"
        "/login — Guardar credenciales del portal\n"
        "/logout — Borrar credenciales\n"
        "/filtros — Elegir compañía y especialidad\n"
        "/check — Buscar huecos ahora mismo\n"
        "/monitor — Activar alertas automáticas\n"
        "/stop — Pausar alertas\n"
        "/interval — Cambiar frecuencia (min)\n"
        "/status — Ver configuración actual\n"
        "/help — Esta ayuda")


# ── /login ─────────────────────────────────────────────────────────────────────

async def login_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> int:
    await _reply(update,
        "🔐 Escribe tu *usuario o DNI* del portal:\n"
        "_(o /cancelar para salir)_")
    return ASK_USER


async def login_user(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["_u"] = update.message.text.strip()
    await _reply(update, "🔑 Ahora tu *contraseña*:")
    return ASK_PASS


async def login_pass(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    tid = update.effective_user.id
    u = ctx.user_data.pop("_u", "")
    p = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    if not u or not p:
        await _reply(update, "❌ Datos vacíos. Usa /login de nuevo.")
        return ConversationHandler.END
    save_credentials(tid, u, p)
    await _reply(update,
        "✅ Credenciales guardadas.\n\n"
        "Ahora usa /filtros para elegir la especialidad a monitorizar.")
    return ConversationHandler.END


async def conv_cancel(update: Update, _: ContextTypes.DEFAULT_TYPE) -> int:
    await _reply(update, "❌ Cancelado.")
    return ConversationHandler.END


# ── /logout ───────────────────────────────────────────────────────────────────

async def cmd_logout(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    tid = update.effective_user.id
    delete_credentials(tid)
    _unschedule(tid)
    clear_earliest(tid)
    save_user_config(tid, {"monitoring": False})
    await _reply(update, "🗑️ Credenciales eliminadas y monitoreo detenido.")


# ── /filtros ──────────────────────────────────────────────────────────────────
# Solo pide compañía + especialidad. Nada más.

async def filtros_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    tid = update.effective_user.id
    cfg = load_user_config(tid)
    f = cfg.get("filters", {})
    await _reply(update,
        f"⚙️ *Configuración actual:*\n"
        f"🏥 Compañía: *{f.get('compania_nombre', 'no configurada')}*\n"
        f"🩺 Especialidad: *{f.get('especialidad_nombre', 'no configurada')}*\n\n"
        "Elige tu *compañía* aseguradora:\n_(o /cancelar para salir)_",
        keyboard=_kbd([v for k, v in COMPANIAS.items() if k]))
    ctx.user_data["_f"] = {}
    return F_COMPANIA


async def filtros_compania(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().upper()
    val = next((k for k, v in COMPANIAS.items() if v.upper() == text), "")
    ctx.user_data["_f"]["compania"] = val
    ctx.user_data["_f"]["compania_nombre"] = COMPANIAS.get(val, text)

    await _reply(update,
        "Elige la *especialidad* a monitorizar:",
        keyboard=_kbd([v for k, v in ESPECIALIDADES.items() if k]))
    return F_ESPECIALIDAD


async def filtros_especialidad(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    tid = update.effective_user.id
    text = update.message.text.strip().upper()
    val = next((k for k, v in ESPECIALIDADES.items()
                if v.upper() == text or k.strip() == text.strip()), "")
    f = ctx.user_data.pop("_f", {})
    f["especialidad"] = val
    f["especialidad_nombre"] = ESPECIALIDADES.get(val, text)

    save_user_config(tid, {"filters": f})
    # Reset stored state so next /check starts fresh
    clear_earliest(tid)

    await _reply(update,
        f"✅ *Filtros guardados:*\n\n"
        f"🏥 Compañía: *{f.get('compania_nombre', '—')}*\n"
        f"🩺 Especialidad: *{f.get('especialidad_nombre', '—')}*\n\n"
        "Usa /check para ver los huecos disponibles ahora,\n"
        "o /monitor para activar las alertas automáticas.")
    return ConversationHandler.END


# ── /check ────────────────────────────────────────────────────────────────────

async def cmd_check(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    tid = update.effective_user.id
    if not load_credentials(tid):
        await _reply(update, "❌ No hay credenciales. Usa /login primero.")
        return
    cfg = load_user_config(tid)
    if not cfg.get("filters", {}).get("especialidad"):
        await _reply(update, "⚠️ No hay especialidad configurada. Usa /filtros primero.")
        return
    msg = await update.message.reply_text("🔍 Buscando huecos disponibles...")
    result = await check_for_user(tid, _notify)
    await msg.edit_text(result, parse_mode=ParseMode.MARKDOWN)


# ── /monitor & /stop ──────────────────────────────────────────────────────────

async def cmd_monitor(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    tid = update.effective_user.id
    if not load_credentials(tid):
        await _reply(update, "❌ No hay credenciales. Usa /login primero.")
        return
    cfg = load_user_config(tid)
    if not cfg.get("filters", {}).get("especialidad"):
        await _reply(update, "⚠️ No hay especialidad configurada. Usa /filtros primero.")
        return

    minutes = cfg.get("interval_minutes", DEFAULT_INTERVAL)
    _reschedule(tid, minutes)
    save_user_config(tid, {"monitoring": True})

    esp = cfg["filters"].get("especialidad_nombre", "")
    await _reply(update,
        f"✅ Monitoreo activado.\n\n"
        f"🩺 Especialidad: *{esp}*\n"
        f"⏱️ Comprobación cada *{minutes} minutos*\n\n"
        "Te aviso si algún médico tiene un hueco más temprano que antes.\n"
        "Usa /stop para pausar · /interval para cambiar la frecuencia.")


async def cmd_stop(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    tid = update.effective_user.id
    _unschedule(tid)
    save_user_config(tid, {"monitoring": False})
    await _reply(update, "⏹️ Monitoreo pausado. Usa /monitor para reactivarlo.")


# ── /interval ─────────────────────────────────────────────────────────────────

async def interval_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> int:
    cfg = load_user_config(update.effective_user.id)
    cur = cfg.get("interval_minutes", DEFAULT_INTERVAL)
    await _reply(update,
        f"⏱️ Intervalo actual: *{cur} minutos*\n\n"
        f"Escribe el nuevo intervalo en minutos (mínimo {MIN_INTERVAL}):\n"
        "_(o /cancelar para salir)_")
    return 0


async def interval_set(update: Update, _: ContextTypes.DEFAULT_TYPE) -> int:
    tid = update.effective_user.id
    try:
        minutes = int(update.message.text.strip())
        if minutes < MIN_INTERVAL:
            await _reply(update, f"❌ Mínimo {MIN_INTERVAL} minutos. Inténtalo de nuevo:")
            return 0
    except ValueError:
        await _reply(update, "❌ Escribe un número entero de minutos:")
        return 0

    save_user_config(tid, {"interval_minutes": minutes})
    if load_user_config(tid).get("monitoring"):
        _reschedule(tid, minutes)
        await _reply(update, f"✅ Intervalo actualizado a *{minutes} minutos* y monitoreo reprogramado.")
    else:
        await _reply(update, f"✅ Intervalo guardado: *{minutes} min*. Activa el monitoreo con /monitor.")
    return ConversationHandler.END


# ── /status ───────────────────────────────────────────────────────────────────

async def cmd_status(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    tid = update.effective_user.id
    has_creds = load_credentials(tid) is not None
    cfg = load_user_config(tid)
    active = cfg.get("monitoring", False) and scheduler.get_job(f"chk_{tid}") is not None
    minutes = cfg.get("interval_minutes", DEFAULT_INTERVAL)
    f = cfg.get("filters", {})

    await _reply(update,
        "*Estado actual:*\n\n"
        f"🔐 Credenciales: {'✅ guardadas' if has_creds else '❌ no configuradas'}\n"
        f"📡 Monitoreo: {'✅ activo' if active else '⏹️ inactivo'}\n"
        f"⏱️ Intervalo: cada *{minutes} minutos*\n\n"
        "*Filtros:*\n"
        f"🏥 Compañía: *{f.get('compania_nombre', 'no configurada')}*\n"
        f"🩺 Especialidad: *{f.get('especialidad_nombre', 'no configurada')}*")


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

    for handler in (
        CommandHandler("start", cmd_start),
        CommandHandler("help", cmd_help),
        login_conv,
        CommandHandler("logout", cmd_logout),
        filtros_conv,
        CommandHandler("check", cmd_check),
        CommandHandler("monitor", cmd_monitor),
        CommandHandler("stop", cmd_stop),
        interval_conv,
        CommandHandler("status", cmd_status),
    ):
        app.add_handler(handler)

    async def on_startup(app: Application) -> None:
        scheduler.start()
        for tid in get_all_monitoring_users():
            cfg = load_user_config(tid)
            _reschedule(tid, cfg.get("interval_minutes", DEFAULT_INTERVAL))
            logger.info("Restored monitoring for user %d", tid)

    async def on_shutdown(app: Application) -> None:
        scheduler.shutdown(wait=False)

    app.post_init = on_startup
    app.post_shutdown = on_shutdown

    logger.info("Bot started. Polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    asyncio.set_event_loop(asyncio.new_event_loop())
    main()
