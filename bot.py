"""
Bot de Telegram — Medastur appointment monitor

Comandos:
  /start      Bienvenida
  /login      Guardar credenciales (conversación)
  /logout     Borrar credenciales
  /filtros    Configurar qué monitorizar (conversación)
  /check      Buscar huecos ahora
  /monitor    Activar monitoreo automático
  /stop       Detener monitoreo
  /interval   Cambiar intervalo en minutos
  /status     Ver configuración actual
  /help       Ayuda
"""
import asyncio
import logging
import os
import re
from datetime import date
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
from scraper import COMPANIAS, ESPECIALIDADES, HORAS
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

# ── States ────────────────────────────────────────────────────────────────────
(ASK_USER, ASK_PASS) = range(2)
(F_COMPANIA, F_ESPECIALIDAD, F_MEDICO, F_CITA_ACTUAL) = range(4)
(ASK_INTERVAL,) = range(1)

DEFAULT_INTERVAL = 30
MIN_INTERVAL = 5

scheduler = AsyncIOScheduler()
_app: Optional[Application] = None


# ── Scheduler ─────────────────────────────────────────────────────────────────

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
    logger.info("Scheduled %s every %d min", jid, minutes)


def _unschedule(tid: int) -> None:
    jid = f"check_{tid}"
    if scheduler.get_job(jid):
        scheduler.remove_job(jid)


# ── UI helpers ────────────────────────────────────────────────────────────────

async def _reply(update: Update, text: str, keyboard: list | None = None) -> None:
    markup = (ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
              if keyboard else ReplyKeyboardRemove())
    await update.message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup
    )


def _kbd(items: list[str], cols: int = 2) -> list[list[str]]:
    return [items[i:i + cols] for i in range(0, len(items), cols)]


# ── /start & /help ─────────────────────────────────────────────────────────────

async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update,
        "👋 *Bot de Citas Medastur*\n\n"
        "Te aviso cuando haya un hueco *más temprano* que tu cita actual.\n\n"
        "1️⃣ /login — credenciales del portal\n"
        "2️⃣ /filtros — especialidad, médico y tu cita actual\n"
        "3️⃣ /monitor — activar alertas automáticas\n\n"
        "/help para todos los comandos.")


async def cmd_help(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update,
        "*Comandos:*\n\n"
        "/login — Guardar credenciales del portal\n"
        "/logout — Borrar credenciales\n"
        "/filtros — Configurar qué buscar\n"
        "/check — Buscar huecos ahora mismo\n"
        "/monitor — Activar alertas automáticas\n"
        "/stop — Detener alertas\n"
        "/interval — Cambiar frecuencia de comprobación\n"
        "/status — Ver configuración actual\n"
        "/help — Esta ayuda")


# ── /login ────────────────────────────────────────────────────────────────────

async def login_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> int:
    await _reply(update,
        "🔐 Escribe tu *usuario o DNI* del portal paciente.medastur.com:\n"
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
    save_user_config(tid, {"monitoring": False})
    await _reply(update, "🗑️ Credenciales eliminadas y monitoreo detenido.")


# ── /filtros ──────────────────────────────────────────────────────────────────

async def filtros_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    tid = update.effective_user.id
    cfg = load_user_config(tid)
    f = cfg.get("filters", {})
    cita = cfg.get("cita_actual", "no configurada")

    await _reply(update,
        f"⚙️ *Configuración actual:*\n"
        f"🏥 Compañía: *{f.get('compania_nombre', 'cualquiera')}*\n"
        f"🩺 Especialidad: *{f.get('especialidad_nombre', 'no configurada')}*\n"
        f"👨‍⚕️ Médico: *{f.get('medico_nombre', 'cualquiera')}*\n"
        f"📅 Tu cita actual: *{cita}*\n\n"
        "Elige tu *compañía* aseguradora:\n_(o /cancelar para salir)_",
        keyboard=_kbd([v for k, v in COMPANIAS.items() if k], cols=1))
    ctx.user_data["_f"] = {}
    return F_COMPANIA


async def filtros_compania(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().upper()
    val = next((k for k, v in COMPANIAS.items() if v.upper() == text), "")
    ctx.user_data["_f"]["compania"] = val
    ctx.user_data["_f"]["compania_nombre"] = COMPANIAS.get(val, text)

    await _reply(update,
        "Elige la *especialidad* a monitorizar:",
        keyboard=_kbd([v for k, v in ESPECIALIDADES.items() if k], cols=1))
    return F_ESPECIALIDAD


async def filtros_especialidad(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().upper()
    val = next((k for k, v in ESPECIALIDADES.items()
                if v.upper() == text or k.strip() == text.strip()), "")
    ctx.user_data["_f"]["especialidad"] = val
    ctx.user_data["_f"]["especialidad_nombre"] = ESPECIALIDADES.get(val, text)

    await _reply(update,
        "¿Quieres un *médico concreto*? Escribe su nombre,\n"
        "o pulsa *cualquiera* para no filtrar por doctor:",
        keyboard=[["cualquiera"]])
    return F_MEDICO


async def filtros_medico(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text.lower() in ("cualquiera", "-", ""):
        ctx.user_data["_f"]["medico"] = ""
        ctx.user_data["_f"]["medico_nombre"] = "cualquiera"
    else:
        ctx.user_data["_f"]["medico"] = text.upper()
        ctx.user_data["_f"]["medico_nombre"] = text.upper()

    await _reply(update,
        "📅 ¿Cuál es tu *cita actual* (la que quieres adelantar)?\n\n"
        "Escribe la fecha en formato *DD/MM/YYYY*\n"
        "_(o 'ninguna' si no tienes cita)_")
    return F_CITA_ACTUAL


async def filtros_cita_actual(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    tid = update.effective_user.id
    text = update.message.text.strip()
    f = ctx.user_data.pop("_f", {})

    cita_iso = None
    if text.lower() not in ("ninguna", "no", "none", "-", ""):
        m = re.match(r"(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{4})", text)
        if not m:
            await _reply(update,
                "❌ Formato no válido. Escribe la fecha como *DD/MM/YYYY* "
                "(ej: 01/07/2026):")
            ctx.user_data["_f"] = f
            return F_CITA_ACTUAL
        d, mo, y = m.groups()
        try:
            cita_iso = date(int(y), int(mo), int(d)).isoformat()
        except ValueError:
            await _reply(update, "❌ Fecha inválida. Inténtalo de nuevo:")
            ctx.user_data["_f"] = f
            return F_CITA_ACTUAL

    save_user_config(tid, {
        "filters": f,
        "cita_actual": cita_iso,
    })

    esp = f.get("especialidad_nombre", "no configurada")
    med = f.get("medico_nombre", "cualquiera")
    cia = f.get("compania_nombre", "cualquiera")
    cita_text = date.fromisoformat(cita_iso).strftime("%d/%m/%Y") if cita_iso else "no configurada"

    await _reply(update,
        f"✅ *Filtros guardados:*\n\n"
        f"🏥 Compañía: *{cia}*\n"
        f"🩺 Especialidad: *{esp}*\n"
        f"👨‍⚕️ Médico: *{med}*\n"
        f"📅 Tu cita actual: *{cita_text}*\n\n"
        "Usa /check para probar ahora,\n"
        "o /monitor para activar las alertas automáticas.")
    return ConversationHandler.END


# ── /check ────────────────────────────────────────────────────────────────────

async def cmd_check(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    tid = update.effective_user.id
    if not load_credentials(tid):
        await _reply(update, "❌ No hay credenciales. Usa /login primero.")
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
        await _reply(update,
            "⚠️ Aún no has configurado la especialidad.\n"
            "Usa /filtros primero.")
        return
    minutes = cfg.get("interval_minutes", DEFAULT_INTERVAL)
    _reschedule(tid, minutes)
    save_user_config(tid, {"monitoring": True})

    cita = cfg.get("cita_actual")
    cita_text = (f"Tu cita actual: *{date.fromisoformat(cita).strftime('%d/%m/%Y')}*"
                 if cita else "_Sin cita de referencia configurada_")

    await _reply(update,
        f"✅ Monitoreo activado — compruebo cada *{minutes} minutos*.\n"
        f"{cita_text}\n\n"
        "Te aviso si aparece un hueco anterior.\n"
        "Usa /stop para pausar · /interval para cambiar la frecuencia.")


async def cmd_stop(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    tid = update.effective_user.id
    _unschedule(tid)
    save_user_config(tid, {"monitoring": False})
    await _reply(update, "⏹️ Monitoreo detenido. Usa /monitor para reactivarlo.")


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
        await _reply(update, "❌ Escribe un número entero:")
        return 0

    save_user_config(tid, {"interval_minutes": minutes})
    if load_user_config(tid).get("monitoring"):
        _reschedule(tid, minutes)
        await _reply(update, f"✅ Intervalo actualizado a *{minutes} minutos* y monitoreo reprogramado.")
    else:
        await _reply(update, f"✅ Intervalo guardado: *{minutes} minutos*.\nActiva el monitoreo con /monitor.")
    return ConversationHandler.END


# ── /status ───────────────────────────────────────────────────────────────────

async def cmd_status(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    tid = update.effective_user.id
    has_creds = load_credentials(tid) is not None
    cfg = load_user_config(tid)
    monitoring = cfg.get("monitoring", False) and scheduler.get_job(f"check_{tid}") is not None
    minutes = cfg.get("interval_minutes", DEFAULT_INTERVAL)
    f = cfg.get("filters", {})
    cita = cfg.get("cita_actual")
    cita_text = date.fromisoformat(cita).strftime("%d/%m/%Y") if cita else "no configurada"

    await _reply(update,
        "*Estado actual:*\n\n"
        f"🔐 Credenciales: {'✅ guardadas' if has_creds else '❌ no configuradas'}\n"
        f"📡 Monitoreo: {'✅ activo' if monitoring else '⏹️ inactivo'}\n"
        f"⏱️ Intervalo: cada *{minutes} minutos*\n\n"
        "*Filtros:*\n"
        f"🏥 Compañía: *{f.get('compania_nombre','cualquiera')}*\n"
        f"🩺 Especialidad: *{f.get('especialidad_nombre','no configurada')}*\n"
        f"👨‍⚕️ Médico: *{f.get('medico_nombre','cualquiera')}*\n"
        f"📅 Tu cita actual: *{cita_text}*")


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
            F_CITA_ACTUAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, filtros_cita_actual), cancel_cmd],
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
