"""
Scraper for paciente.medastur.com/autoservicio.aspx

POST cascade:
  1. GET  /autoservicio.aspx          → ViewState + initial dropdowns
  2. POST __EVENTTARGET=ddlCompania   → reload especialidades
  3. POST __EVENTTARGET=ddlEspecialidad → reload ddlMedico
  4. POST btnBuscarCitasLibres        → result cards
"""
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

BASE_URL  = "https://paciente.medastur.com"
LOGIN_URL = f"{BASE_URL}/Login.aspx"
AUTO_URL  = f"{BASE_URL}/autoservicio.aspx"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-ES,es;q=0.9",
    "Referer": BASE_URL,
}

logger = logging.getLogger(__name__)

# ── Catalogues (from live inspection) ────────────────────────────────────────

COMPANIAS = {
    "":      "- Elige compañía -",
    "00999": "CLIENTES PARTICULARES",
    "00001": "ASISA",
    "00323": "CIGNA HEALTHCARE ESPAÑA",
    "00030": "SANITAS",
}

ESPECIALIDADES = {
    "":     "- Elige Especialidad -",
    "03  ": "ALERGOLOGIA",
    "09  ": "CARDIOLOGIA",
    "0901": "CARDIOLOGIA INFANTIL",
    "52  ": "CIRUGIA BARIATRICA Y METABOLICA",
    "10  ": "CIRUGIA CARDIOVASCULAR",
    "11  ": "CIRUGIA GENERAL Y DEL APARATO DIGESTIVO",
    "13  ": "CIRUGIA PEDIATRICA",
    "14  ": "CIRUGIA PLASTICA Y REPARADORA",
    "07  ": "CIRUGIA VASCULAR",
    "16  ": "DERMATOLOGIA",
    "4306": "DIETETICA Y NUTRICION",
    "08  ": "DIGESTIVO",
    "21  ": "HEMATOLOGIA Y HEMOTERAPIA",
    "4307": "LOGOPEDIA",
    "01  ": "MEDICINA GENERAL",
    "02  ": "MEDICINA INTERNA",
    "23  ": "NEFROLOGIA",
    "24  ": "NEUMOLOGIA",
    "25  ": "NEUROCIRUGIA",
    "26  ": "NEUROLOGIA",
    "27  ": "OBSTETRICIA Y GINECOLOGIA",
    "28  ": "OFTALMOLOGIA",
    "29  ": "ONCOLOGIA MEDICA",
    "30  ": "OTORRINOLARINGOLOGIA",
    "4308": "PEDIATRIA",
    "31  ": "PSIQUIATRIA",
    "4309": "PSICOLOGIA",
    "32  ": "REUMATOLOGIA",
    "33  ": "TRAUMATOLOGIA Y CIRUGIA ORTOPEDICA",
    "34  ": "UROLOGIA",
    "4310": "FISIOTERAPIA",
}

HORAS = [f"{h:02d}:{m:02d}" for h in range(8, 21) for m in (0, 30)] + ["20:30"]

_MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}

# Regex for Spanish long date: "martes, 7 de julio de 2026 a las 17:35"
_DATE_RE = re.compile(
    r"(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})\s+a\s+las\s+(\d{1,2}:\d{2})",
    re.IGNORECASE,
)


# ── Data class ────────────────────────────────────────────────────────────────

@dataclass
class Slot:
    fecha_dt: date        # parsed date for comparison
    hora: str             # "17:35"
    doctor: str
    centro: str
    fecha_text: str       # "martes, 7 de julio de 2026"

    def key(self) -> str:
        return f"{self.fecha_dt.isoformat()}|{self.hora}|{self.doctor}"

    def to_dict(self) -> dict:
        return {
            "fecha_dt": self.fecha_dt.isoformat(),
            "hora": self.hora,
            "doctor": self.doctor,
            "centro": self.centro,
            "fecha_text": self.fecha_text,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Slot":
        return cls(
            fecha_dt=date.fromisoformat(d["fecha_dt"]),
            hora=d["hora"],
            doctor=d["doctor"],
            centro=d["centro"],
            fecha_text=d.get("fecha_text", d["fecha_dt"]),
        )

    def __str__(self) -> str:
        lines = [f"📅 {self.fecha_text.capitalize()}  🕐 {self.hora}"]
        if self.doctor:
            lines.append(f"👨‍⚕️ {self.doctor}")
        if self.centro:
            lines.append(f"📍 {self.centro}")
        return "\n".join(lines)


def parse_spanish_date(text: str) -> tuple[date, str, str] | None:
    """Parse 'martes, 7 de julio de 2026 a las 17:35' → (date, hora, fecha_text)."""
    m = _DATE_RE.search(text)
    if not m:
        return None
    dia, mes_str, anio, hora = m.groups()
    mes = _MESES.get(mes_str.lower())
    if not mes:
        return None
    try:
        dt = date(int(anio), mes, int(dia))
    except ValueError:
        return None
    # Capture the date portion text (before "a las")
    fecha_text = text[:m.start() + text[m.start():].index(" a las")].strip().lstrip(",").strip()
    return dt, hora, fecha_text


# ── Scraper ───────────────────────────────────────────────────────────────────

class MedasturScraper:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._logged_in = False

    # ── Login ─────────────────────────────────────────────────────────────────

    def login(self, username: str, password: str) -> bool:
        try:
            resp = self.session.get(LOGIN_URL, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error("Error fetching login page: %s", e)
            return False

        soup = BeautifulSoup(resp.text, "html.parser")
        hidden = self._aspnet_fields(soup)
        user_f, pass_f, submit = self._discover_login_fields(soup)

        if not user_f or not pass_f:
            logger.error("Login fields not found in page")
            return False

        payload = {user_f: username, pass_f: password, **hidden}
        if submit:
            payload.update(submit)

        try:
            r2 = self.session.post(LOGIN_URL, data=payload, timeout=20)
            r2.raise_for_status()
        except requests.RequestException as e:
            logger.error("Login POST error: %s", e)
            return False

        if "login" in r2.url.lower() and self._has_error(r2.text):
            logger.warning("Login rejected")
            return False

        self._logged_in = True
        logger.info("Logged in as %s", username)
        return True

    def _discover_login_fields(self, soup):
        user_f = pass_f = None
        submit = {}
        for inp in soup.find_all("input"):
            t = (inp.get("type") or "text").lower()
            name = inp.get("name", "")
            hint = (inp.get("id", "") + inp.get("placeholder", "")).lower()
            if t == "password":
                pass_f = name
            elif t in ("text", "email") and any(
                k in hint for k in ("user", "nif", "dni", "usuario", "email", "correo", "login")
            ):
                user_f = name
            elif t == "submit":
                submit = {name: inp.get("value", "")}
        if not user_f:
            for inp in soup.find_all("input", {"type": ["text", "email"]}):
                n = inp.get("name", "")
                if n and not n.startswith("__"):
                    user_f = n
                    break
        return user_f, pass_f, submit

    def _has_error(self, html: str) -> bool:
        lower = html.lower()
        return any(k in lower for k in (
            "contraseña incorrecta", "usuario incorrecto", "credenciales",
            "invalid", "no válido", "acceso denegado",
        ))

    # ── Search ────────────────────────────────────────────────────────────────

    def search_slots(self, filters: dict) -> list[Slot]:
        """
        filters keys: compania, especialidad,
          fecha_desde (dd/MM/yyyy, default today),
          hora_desde (default 08:00), hora_hasta (default 20:30)
        """
        if not self._logged_in:
            raise RuntimeError("Not logged in")

        compania     = filters.get("compania", "")
        especialidad = filters.get("especialidad", "")
        fecha        = filters.get("fecha_desde", date.today().strftime("%d/%m/%Y"))
        hora_desde   = filters.get("hora_desde", "08:00")
        hora_hasta   = filters.get("hora_hasta", "20:30")

        # Step 1: base page
        soup, fields = self._get_auto()
        if soup is None:
            return []

        # Step 2: PostBack compania → reloads centros + especialidades
        if compania:
            fields["ddlCompania"] = compania
            soup, fields = self._postback("ddlCompania", fields)
            if soup is None:
                return []
            logger.info("After ddlCompania: centro=%r provincia=%r localidad=%r",
                        fields.get("ddlCentro"), fields.get("ddlProvincia"),
                        fields.get("ddlLocalidad"))
            # Log available especialidad options to verify our code matches
            esp_sel = soup.find("select", {"name": "ddlEspecialidad"})
            if esp_sel:
                esp_opts = [(o.get("value",""), o.get_text(strip=True)) for o in esp_sel.find_all("option")]
                logger.info("ddlEspecialidad options: %r", esp_opts)

        # Step 3: PostBack especialidad → reloads ddlMedico
        if especialidad:
            fields["ddlEspecialidad"] = especialidad
            soup, fields = self._postback("ddlEspecialidad", fields)
            if soup is None:
                return []

        # Step 4: PostBack ddlMedico → select "CUALQUIER MÉDICO"
        medico = ""
        if soup:
            medico_sel = soup.find("select", {"name": "ddlMedico"})
            if medico_sel:
                opts = medico_sel.find_all("option")
                logger.info("ddlMedico options: %r",
                            [(o.get("value",""), o.get_text(strip=True)) for o in opts[:8]])
                cualquier = next(
                    (o for o in opts if "cualquier" in o.get_text(strip=True).lower()),
                    opts[0] if opts else None,
                )
                if cualquier:
                    medico = cualquier.get("value", "")
                    logger.info("ddlMedico → %r (%r)", cualquier.get_text(strip=True), medico)

        if medico:
            fields["ddlMedico"] = medico
            soup, fields = self._postback("ddlMedico", fields)
            if soup is None:
                return []

        # Step 5: final search POST — use form values from PostBacks, only override date/time
        fields["ddlCompania"]         = compania
        fields["ddlEspecialidad"]     = especialidad
        fields["ddlMedico"]           = medico
        fields["nuevaCita_Fecha"]     = fecha
        fields["horapreferente"]      = hora_desde
        fields["horapreferentehasta"] = hora_hasta
        fields["ddlLanguages"]        = "es"
        fields["nuevaCita_id"]        = ""
        fields["__EVENTTARGET"]       = "btnBuscarCitasLibres"
        fields["__EVENTARGUMENT"]     = ""
        fields["__LASTFOCUS"]         = ""
        fields.pop("btnBuscarCitasLibres", None)  # button uses __doPostBack, not submit value
        logger.info("Search POST fields subset: compania=%r esp=%r medico=%r centro=%r",
                    compania, especialidad, medico, fields.get("ddlCentro"))

        try:
            resp = self.session.post(AUTO_URL, data=fields, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error("Search POST error: %s", e)
            return []

        page_plain = BeautifulSoup(resp.text, "html.parser").get_text(separator=" ", strip=True)
        logger.info("Search page text (last 1500 chars): %s", page_plain[-1500:].replace("\n", " "))
        slots = self._parse_results(resp.text)
        logger.info("Found %d slots", len(slots))
        return slots

    def _get_auto(self):
        try:
            resp = self.session.get(AUTO_URL, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error("GET autoservicio error: %s", e)
            return None, {}
        soup = BeautifulSoup(resp.text, "html.parser")
        return soup, self._form_state(soup)

    def _postback(self, target: str, fields: dict):
        payload = {**fields, "__EVENTTARGET": target, "__EVENTARGUMENT": ""}
        try:
            resp = self.session.post(AUTO_URL, data=payload, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error("PostBack %s error: %s", target, e)
            return None, fields
        soup = BeautifulSoup(resp.text, "html.parser")
        new = self._form_state(soup)
        for k, v in fields.items():
            new.setdefault(k, v)
        return soup, new

    def _form_state(self, soup: BeautifulSoup) -> dict:
        state = self._aspnet_fields(soup)
        for sel in soup.find_all("select"):
            name = sel.get("name", "")
            if name:
                opt = sel.find("option", selected=True) or sel.find("option")
                state[name] = opt["value"] if opt else ""
        for inp in soup.find_all("input"):
            if (inp.get("type") or "").lower() not in ("hidden", "submit", "button", "image"):
                name = inp.get("name", "")
                if name:
                    state[name] = inp.get("value", "")
        return state

    def _aspnet_fields(self, soup: BeautifulSoup) -> dict:
        fields = {}
        for name in ("__VIEWSTATE", "__EVENTVALIDATION", "__VIEWSTATEGENERATOR",
                     "__EVENTTARGET", "__EVENTARGUMENT", "__LASTFOCUS"):
            tag = soup.find("input", {"name": name})
            if tag:
                fields[name] = tag.get("value", "")
        return fields

    # ── Parse result cards ────────────────────────────────────────────────────

    def _parse_results(self, html: str) -> list[Slot]:
        soup = BeautifulSoup(html, "html.parser")
        slots: list[Slot] = []

        # Strategy 1: find every element whose text contains Spanish date pattern
        # Cards are typically small divs/li containing center + doctor + date
        seen_keys: set[str] = set()

        for elem in soup.find_all(["div", "li", "article", "tr"]):
            text = elem.get_text(separator=" ", strip=True)
            parsed = parse_spanish_date(text)
            if not parsed:
                continue

            # Avoid matching parent containers that include all child cards
            child_matches = sum(
                1 for c in elem.find_all(["div", "li", "article"])
                if parse_spanish_date(c.get_text(separator=" ", strip=True))
            )
            if child_matches > 0:
                continue  # this is a container, skip

            dt, hora, fecha_text = parsed

            # Extract doctor and center from the same card text
            doctor = self._extract_doctor(text)
            centro = self._extract_centro(text)

            slot = Slot(fecha_dt=dt, hora=hora, doctor=doctor,
                        centro=centro, fecha_text=fecha_text)
            if slot.key() not in seen_keys:
                seen_keys.add(slot.key())
                slots.append(slot)

        if slots:
            return sorted(slots, key=lambda s: (s.fecha_dt, s.hora))

        # Strategy 2: table rows (fallback)
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue
            headers = [th.get_text(strip=True).lower()
                       for th in rows[0].find_all(["th", "td"])]
            if not any(k in " ".join(headers) for k in ("fecha", "hora", "día")):
                continue
            for row in rows[1:]:
                cells = [td.get_text(strip=True) for td in row.find_all("td")]
                text = " ".join(cells)
                parsed = parse_spanish_date(text)
                if parsed:
                    dt, hora, fecha_text = parsed
                    slot = Slot(fecha_dt=dt, hora=hora,
                                doctor=self._extract_doctor(text),
                                centro=self._extract_centro(text),
                                fecha_text=fecha_text)
                    if slot.key() not in seen_keys:
                        seen_keys.add(slot.key())
                        slots.append(slot)
            if slots:
                return sorted(slots, key=lambda s: (s.fecha_dt, s.hora))

        # Detect explicit "no results" message
        page_text = soup.get_text(strip=True).lower()
        no_result_kw = ("no hay citas", "no existen citas", "no se han encontrado",
                        "sin disponibilidad", "no hay disponibilidad",
                        "debe buscar la disponibilidad")
        if any(k in page_text for k in no_result_kw):
            return []

        logger.info("No slots parsed. Page text sample: %s", page_text[-800:])
        return []

    def _extract_doctor(self, text: str) -> str:
        # Doctor names are typically ALL CAPS "APELLIDO, NOMBRE" before the date
        # Remove the date portion and look for capitalised words
        text_clean = _DATE_RE.sub("", text)
        # Remove center keywords
        text_clean = re.sub(r"centro\s+médico\s+\w+", "", text_clean, flags=re.I)
        # Find all-caps multi-word sequences (doctor names)
        m = re.search(r"[A-ZÁÉÍÓÚÜÑ]{2,}[\s,]+[A-ZÁÉÍÓÚÜÑ]{2,}(?:[\s,]+[A-ZÁÉÍÓÚÜÑ]{2,})?", text_clean)
        return m.group().strip(" ,") if m else ""

    def _extract_centro(self, text: str) -> str:
        m = re.search(r"CENTRO\s+MÉDICO\s+\w+|CLINICA\s+\w+|HOSPITAL\s+\w+", text, re.I)
        return m.group().strip() if m else ""

    # ── Helpers ───────────────────────────────────────────────────────────────

    def logout(self) -> None:
        try:
            self.session.get(f"{BASE_URL}/CerrarSesion.aspx", timeout=10)
        except requests.RequestException:
            pass
        self.session.cookies.clear()
        self._logged_in = False
