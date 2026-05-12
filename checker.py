"""
Check logic:
  - Busca huecos disponibles para la especialidad/médico configurados
  - Solo interesa lo que sea ANTERIOR a la cita que ya tiene el usuario
  - Notifica si aparece un hueco nuevo más temprano
"""
import logging
from datetime import date
from typing import Callable, Awaitable

from scraper import MedasturScraper, Slot, parse_spanish_date
from storage import (
    load_credentials, load_user_config,
    load_known_slots, save_known_slots,
)

logger = logging.getLogger(__name__)


async def check_for_user(
    telegram_id: int,
    notify: Callable[[int, str], Awaitable[None]],
) -> str:
    creds = load_credentials(telegram_id)
    if not creds:
        return "❌ No hay credenciales. Usa /login primero."

    cfg = load_user_config(telegram_id)
    filters = cfg.get("filters", {})

    # Parse the user's current appointment date (the "deadline")
    cita_actual_str: str | None = cfg.get("cita_actual")
    cita_actual: date | None = None
    if cita_actual_str:
        try:
            cita_actual = date.fromisoformat(cita_actual_str)
        except ValueError:
            pass

    username, password = creds
    scraper = MedasturScraper()
    try:
        if not scraper.login(username, password):
            return "❌ Credenciales incorrectas. Usa /login para actualizarlas."
        all_slots = scraper.search_slots(filters)
    except Exception as e:
        logger.exception("Error checking user %d", telegram_id)
        return f"❌ Error al comprobar huecos: {e}"
    finally:
        scraper.logout()

    if not all_slots:
        return (
            "📭 No hay huecos disponibles con los filtros actuales.\n"
            + _filters_summary(filters, cita_actual)
        )

    # If user has a current appointment, only care about earlier slots
    if cita_actual:
        earlier = [s for s in all_slots if s.fecha_dt < cita_actual]
    else:
        earlier = all_slots

    # Compare with previously known slots
    prev_keys = {s["fecha_dt"] + "|" + s["hora"] + "|" + s["doctor"]
                 for s in load_known_slots(telegram_id)}
    new_slots = [s for s in earlier if s.key() not in prev_keys]

    # Save ALL found slots (not just earlier ones) so we don't re-alert
    save_known_slots(telegram_id, [s.to_dict() for s in all_slots])

    if new_slots:
        if cita_actual:
            lines = [
                f"🔔 *¡Nuevo hueco más temprano disponible!*\n",
                f"_(Tu cita actual: {cita_actual.strftime('%d/%m/%Y')})_\n",
            ]
        else:
            lines = [f"🔔 *{len(new_slots)} hueco(s) nuevo(s):*\n"]
        for s in new_slots:
            lines.append(str(s))
            lines.append("")
        await notify(telegram_id, "\n".join(lines))

    # Build status reply
    if cita_actual and not earlier:
        earliest = all_slots[0]
        return (
            f"✅ Comprobado. No hay huecos antes de tu cita actual "
            f"(*{cita_actual.strftime('%d/%m/%Y')}*).\n\n"
            f"El más temprano disponible ahora:\n{earliest}"
        )

    if cita_actual and earlier:
        lines = [
            f"📋 *{len(earlier)} hueco(s) anteriores a tu cita ({cita_actual.strftime('%d/%m/%Y')}):*\n"
        ]
        for s in earlier:
            lines.append(str(s))
            lines.append("")
        return "\n".join(lines)

    # No cita_actual set
    lines = [f"📋 *{len(all_slots)} hueco(s) disponibles:*\n"]
    for s in all_slots[:10]:   # show max 10
        lines.append(str(s))
        lines.append("")
    if len(all_slots) > 10:
        lines.append(f"_...y {len(all_slots) - 10} más._")
    return "\n".join(lines)


def _filters_summary(filters: dict, cita_actual: date | None) -> str:
    parts = []
    if filters.get("especialidad_nombre"):
        parts.append(f"Especialidad: {filters['especialidad_nombre']}")
    if filters.get("medico_nombre") and filters["medico_nombre"] != "cualquiera":
        parts.append(f"Médico: {filters['medico_nombre']}")
    if cita_actual:
        parts.append(f"Buscando antes del: {cita_actual.strftime('%d/%m/%Y')}")
    return "\n".join(parts)
