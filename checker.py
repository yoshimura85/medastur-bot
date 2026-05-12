"""Core check logic: login → search → diff → notify."""
import logging
from typing import Callable, Awaitable

from scraper import MedasturScraper, Slot
from storage import (
    load_credentials,
    load_user_config,
    load_known_slots,
    save_known_slots,
)

logger = logging.getLogger(__name__)


async def check_for_user(
    telegram_id: int,
    notify: Callable[[int, str], Awaitable[None]],
) -> str:
    creds = load_credentials(telegram_id)
    if not creds:
        return "❌ No hay credenciales. Usa /login primero."

    username, password = creds
    cfg = load_user_config(telegram_id)
    filters = cfg.get("filters", {})

    scraper = MedasturScraper()
    try:
        if not scraper.login(username, password):
            return "❌ Credenciales incorrectas. Usa /login para actualizarlas."

        slots = scraper.search_available_slots(filters)
    except Exception as e:
        logger.exception("Error checking user %d", telegram_id)
        return f"❌ Error al comprobar citas: {e}"
    finally:
        scraper.logout()

    prev_keys = {s["fecha"] + "|" + s["hora"] + "|" + s["doctor"]
                 for s in load_known_slots(telegram_id)}
    new_slots = [s for s in slots if s.key() not in prev_keys]
    save_known_slots(telegram_id, [s.to_dict() for s in slots])

    if new_slots:
        lines = [f"🔔 *{len(new_slots)} hueco(s) nuevo(s) disponible(s):*\n"]
        for s in new_slots:
            lines.append(str(s))
            lines.append("")
        await notify(telegram_id, "\n".join(lines))

    if not slots:
        return "📭 No hay huecos disponibles con los filtros actuales."

    lines = [f"📋 *{len(slots)} hueco(s) disponible(s):*\n"]
    for s in slots:
        lines.append(str(s))
        lines.append("")
    return "\n".join(lines)
