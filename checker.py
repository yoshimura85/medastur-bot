"""Core appointment-checking logic: login, fetch, diff, notify."""
import logging
from typing import Callable, Awaitable

from scraper import Appointment, MedasturScraper
from storage import (
    load_credentials,
    load_known_appointments,
    save_known_appointments,
)

logger = logging.getLogger(__name__)


async def check_appointments_for_user(
    telegram_id: int,
    notify: Callable[[int, str], Awaitable[None]],
) -> str:
    """
    Check appointments for a single user. Calls notify() if new slots appear.
    Returns a human-readable status string.
    """
    creds = load_credentials(telegram_id)
    if not creds:
        return "❌ No hay credenciales guardadas. Usa /login primero."

    username, password = creds
    scraper = MedasturScraper()

    try:
        logged_in = scraper.login(username, password)
    except Exception as e:
        logger.exception("Login error for user %d", telegram_id)
        return f"❌ Error al iniciar sesión: {e}"

    if not logged_in:
        return "❌ Credenciales incorrectas. Usa /login para actualizarlas."

    try:
        current = scraper.get_appointments()
    except Exception as e:
        logger.exception("Error fetching appointments for user %d", telegram_id)
        return f"❌ Error al obtener citas: {e}"
    finally:
        scraper.logout()

    previous_keys = {
        a["doctor"] + "|" + a["date"] + "|" + a["time"]
        for a in load_known_appointments(telegram_id)
    }

    new_appointments = [a for a in current if a.key() not in previous_keys]
    save_known_appointments(telegram_id, [a.to_dict() for a in current])

    if new_appointments:
        lines = [f"🔔 *{len(new_appointments)} cita(s) nueva(s) disponible(s):*\n"]
        for appt in new_appointments:
            lines.append(str(appt))
            lines.append("")
        await notify(telegram_id, "\n".join(lines))

    if not current:
        return "📭 No hay citas disponibles en este momento."

    lines = [f"📋 *{len(current)} cita(s) encontrada(s):*\n"]
    for appt in current:
        lines.append(str(appt))
        lines.append("")
    return "\n".join(lines)
