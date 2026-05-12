"""
Lógica de monitoreo:
  - Busca todos los huecos disponibles para la especialidad configurada
  - Agrupa por doctor y guarda el hueco más temprano de cada uno
  - Notifica si algún doctor tiene ahora un hueco ANTERIOR al que tenía antes
"""
import logging
from datetime import date
from typing import Callable, Awaitable

from scraper import MedasturScraper, Slot
from storage import load_credentials, load_user_config, load_earliest, save_earliest

logger = logging.getLogger(__name__)


def _earliest_per_doctor(slots: list[Slot]) -> dict[str, Slot]:
    """Devuelve el hueco más temprano por cada doctor."""
    best: dict[str, Slot] = {}
    for s in slots:
        key = s.doctor or "SIN NOMBRE"
        if key not in best or (s.fecha_dt, s.hora) < (best[key].fecha_dt, best[key].hora):
            best[key] = s
    return best


async def check_for_user(
    telegram_id: int,
    notify: Callable[[int, str], Awaitable[None]],
) -> str:
    creds = load_credentials(telegram_id)
    if not creds:
        return "❌ No hay credenciales. Usa /login primero."

    cfg = load_user_config(telegram_id)
    filters = cfg.get("filters", {})
    if not filters.get("especialidad"):
        return "⚠️ No hay especialidad configurada. Usa /filtros primero."

    username, password = creds
    scraper = MedasturScraper()
    try:
        if not scraper.login(username, password):
            return "❌ Credenciales incorrectas. Usa /login para actualizarlas."
        all_slots = scraper.search_slots(filters)
    except Exception as e:
        logger.exception("Error checking user %d", telegram_id)
        return f"❌ Error al buscar huecos: {e}"
    finally:
        scraper.logout()

    if not all_slots:
        esp = filters.get("especialidad_nombre", "")
        return f"📭 No hay huecos disponibles para *{esp}* con los filtros actuales."

    current_best = _earliest_per_doctor(all_slots)
    previous_best: dict[str, dict] = load_earliest(telegram_id)

    # Detect improvements: doctor has an earlier slot than before
    alerts: list[str] = []
    for doctor, slot in current_best.items():
        prev = previous_best.get(doctor)
        if prev is None:
            # New doctor seen for first time — no alert, just record
            continue
        prev_date = date.fromisoformat(prev["fecha_dt"])
        if slot.fecha_dt < prev_date:
            alerts.append(
                f"👨‍⚕️ *{doctor}*\n"
                f"  Antes:  {prev_date.strftime('%d/%m/%Y')} {prev['hora']}\n"
                f"  ✅ Ahora: {slot.fecha_dt.strftime('%d/%m/%Y')} {slot.hora}"
            )

    # Persist current best
    save_earliest(telegram_id, {
        doc: s.to_dict() for doc, s in current_best.items()
    })

    # Send alert if improvements found
    if alerts:
        esp = filters.get("especialidad_nombre", "")
        msg = f"🔔 *¡Nuevo hueco anterior disponible!*\n_{esp}_\n\n" + "\n\n".join(alerts)
        await notify(telegram_id, msg)

    # Build the summary reply
    esp = filters.get("especialidad_nombre", filters.get("especialidad", ""))
    lines = [f"📋 *{esp}* — hueco más temprano por doctor:\n"]
    for doctor, slot in sorted(current_best.items(),
                                key=lambda x: (x[1].fecha_dt, x[1].hora)):
        lines.append(
            f"👨‍⚕️ *{doctor}*\n"
            f"   📅 {slot.fecha_text.capitalize()}  🕐 {slot.hora}\n"
        )
    return "\n".join(lines)
