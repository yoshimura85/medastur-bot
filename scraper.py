"""
Scraper for paciente.medastur.com/autoservicio.aspx

Flow:
  1. GET /Login.aspx  → extract ViewState, discover field names
  2. POST credentials → session established
  3. GET /autoservicio.aspx → extract ViewState + current dropdown values
  4. POST __EVENTTARGET=ddlCompania  → reload especialidades (cascade)
  5. POST __EVENTTARGET=ddlEspecialidad → reload ddlMedico (cascade)
  6. POST btnBuscarCitasLibres → search results
  7. Parse result table
"""
import logging
import re
from dataclasses import dataclass
from datetime import date

import requests
from bs4 import BeautifulSoup

BASE_URL  = "https://paciente.medastur.com"
LOGIN_URL = f"{BASE_URL}/Login.aspx"
HOME_URL  = f"{BASE_URL}/Inicio.aspx"
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

# ── Known option catalogues (from live inspection) ────────────────────────────

COMPANIAS = {
    "": "- Elige compañia -",
    "00999": "CLIENTES PARTICULARES",
    "00001": "ASISA",
    "00323": "CIGNA HEALTHCARE ESPAÑA",
    "00030": "SANITAS",
}

ESPECIALIDADES_DEFAULT = {
    "": "- Elige Especialidad -",
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
    "4306": "DIETÉTICA Y NUTRICIÓN",
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


# ── Data class ────────────────────────────────────────────────────────────────

@dataclass
class Slot:
    fecha: str
    hora: str
    doctor: str
    consulta: str
    centro: str

    def key(self) -> str:
        return f"{self.fecha}|{self.hora}|{self.doctor}"

    def to_dict(self) -> dict:
        return {"fecha": self.fecha, "hora": self.hora,
                "doctor": self.doctor, "consulta": self.consulta,
                "centro": self.centro}

    @classmethod
    def from_dict(cls, d: dict) -> "Slot":
        return cls(**d)

    def __str__(self) -> str:
        lines = [f"📅 {self.fecha}  🕐 {self.hora}"]
        if self.doctor:
            lines.append(f"👨‍⚕️ {self.doctor}")
        if self.consulta:
            lines.append(f"🏥 {self.consulta}")
        if self.centro:
            lines.append(f"📍 {self.centro}")
        return "\n".join(lines)


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
        user_field, pass_field, submit = self._discover_login_fields(soup)

        if not user_field or not pass_field:
            logger.error("Could not detect login form fields")
            return False

        payload = {
            user_field: username,
            pass_field: password,
            **hidden,
        }
        if submit:
            payload.update(submit)

        try:
            resp2 = self.session.post(LOGIN_URL, data=payload, timeout=20,
                                      allow_redirects=True)
            resp2.raise_for_status()
        except requests.RequestException as e:
            logger.error("Login POST error: %s", e)
            return False

        if "login" in resp2.url.lower() and self._page_has_error(resp2.text):
            logger.warning("Login rejected: bad credentials")
            return False

        self._logged_in = True
        logger.info("Logged in as %s", username)
        return True

    def _discover_login_fields(self, soup: BeautifulSoup):
        user_field = pass_field = None
        submit = {}
        for inp in soup.find_all("input"):
            t = (inp.get("type", "text") or "text").lower()
            name = inp.get("name", "")
            iid = (inp.get("id", "") + inp.get("placeholder", "")).lower()
            if t == "password":
                pass_field = name
            elif t in ("text", "email") and any(
                k in iid for k in ("user", "login", "nif", "dni", "usuario", "email", "correo")
            ):
                user_field = name
            elif t == "submit":
                submit = {name: inp.get("value", "")}
        if not user_field:
            for inp in soup.find_all("input", {"type": ["text", "email"]}):
                n = inp.get("name", "")
                if n and not n.startswith("__"):
                    user_field = n
                    break
        return user_field, pass_field, submit

    def _page_has_error(self, html: str) -> bool:
        return any(k in html.lower() for k in (
            "contraseña incorrecta", "usuario incorrecto", "credenciales",
            "invalid", "no válido", "acceso denegado", "error"
        ))

    # ── Search available slots ────────────────────────────────────────────────

    def search_available_slots(self, filters: dict) -> list[Slot]:
        """
        filters keys (all optional):
          compania, especialidad, medico, centro, provincia, localidad,
          fecha_desde (dd/MM/yyyy), hora_desde (HH:MM), hora_hasta (HH:MM)
        """
        if not self._logged_in:
            raise RuntimeError("Not logged in")

        compania    = filters.get("compania", "")
        especialidad = filters.get("especialidad", "")
        medico      = filters.get("medico", "")
        centro      = filters.get("centro", "30061")
        provincia   = filters.get("provincia", "0033")
        localidad   = filters.get("localidad", "0447   ")
        fecha       = filters.get("fecha_desde", date.today().strftime("%d/%m/%Y"))
        hora_desde  = filters.get("hora_desde", "08:00")
        hora_hasta  = filters.get("hora_hasta", "20:30")

        # Step 1: GET base page
        soup, fields = self._get_auto_page()
        if soup is None:
            return []

        # Step 2: cascade ddlCompania if needed
        if compania:
            fields["ddlCompania"] = compania
            soup, fields = self._postback("ddlCompania", fields)
            if soup is None:
                return []

        # Step 3: cascade ddlEspecialidad if needed
        if especialidad:
            fields["ddlEspecialidad"] = especialidad
            soup, fields = self._postback("ddlEspecialidad", fields)
            if soup is None:
                return []

        # Step 4: set remaining fields and submit
        fields.update({
            "ddlCompania":       compania,
            "ddlEspecialidad":   especialidad,
            "ddlMedico":         medico,
            "ddlCentro":         centro,
            "ddlProvincia":      provincia,
            "ddlLocalidad":      localidad,
            "nuevaCita_Fecha":   fecha,
            "horapreferente":    hora_desde,
            "horapreferentehasta": hora_hasta,
            "ddlAtencion":       fields.get("ddlAtencion", ""),
            "ddlLanguages":      "es",
            "nuevaCita_id":      "",
            "__EVENTTARGET":     "",
            "__EVENTARGUMENT":   "",
            "__LASTFOCUS":       "",
            "btnBuscarCitasLibres": "Buscar citas libres",
        })

        try:
            resp = self.session.post(AUTO_URL, data=fields, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error("Search POST error: %s", e)
            return []

        return self._parse_results(resp.text)

    def _get_auto_page(self) -> tuple:
        try:
            resp = self.session.get(AUTO_URL, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error("Error fetching autoservicio: %s", e)
            return None, {}
        soup = BeautifulSoup(resp.text, "html.parser")
        return soup, self._form_state(soup)

    def _postback(self, target: str, fields: dict) -> tuple:
        payload = {**fields, "__EVENTTARGET": target, "__EVENTARGUMENT": ""}
        try:
            resp = self.session.post(AUTO_URL, data=payload, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error("PostBack error for %s: %s", target, e)
            return None, fields
        soup = BeautifulSoup(resp.text, "html.parser")
        new_fields = self._form_state(soup)
        # carry over values that weren't reset
        for k, v in fields.items():
            if k not in new_fields:
                new_fields[k] = v
        return soup, new_fields

    def _form_state(self, soup: BeautifulSoup) -> dict:
        """Extract all hidden fields + current dropdown selected values."""
        state = self._aspnet_fields(soup)
        for sel in soup.find_all("select"):
            name = sel.get("name", "")
            if name:
                selected = sel.find("option", selected=True)
                state[name] = selected["value"] if selected else (
                    sel.find("option")["value"] if sel.find("option") else ""
                )
        for inp in soup.find_all("input"):
            if inp.get("type", "").lower() not in ("hidden", "submit", "button"):
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

    # ── Parse results ─────────────────────────────────────────────────────────

    def _parse_results(self, html: str) -> list[Slot]:
        soup = BeautifulSoup(html, "html.parser")
        slots: list[Slot] = []

        # Look for result rows — typically inside a table or update panel
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue
            headers = [th.get_text(strip=True).lower()
                       for th in rows[0].find_all(["th", "td"])]
            # Must have at least a date/hora column
            if not any(k in " ".join(headers) for k in ("fecha", "hora", "día")):
                continue
            col = self._map_cols(headers)
            for row in rows[1:]:
                cells = [td.get_text(strip=True) for td in row.find_all("td")]
                if not cells or all(c == "" for c in cells):
                    continue
                slot = self._cells_to_slot(cells, col)
                if slot:
                    slots.append(slot)
            if slots:
                return slots

        # Fallback: look for div-based result cards
        for card in soup.find_all("div", class_=re.compile(r"cita|slot|result|disp", re.I)):
            text = card.get_text(separator=" ", strip=True)
            slot = self._text_to_slot(text)
            if slot:
                slots.append(slot)

        # Detect "no results" message
        page_text = soup.get_text(strip=True).lower()
        no_results_keywords = (
            "no hay citas", "no existen citas", "no se han encontrado",
            "sin disponibilidad", "no disponible", "no hay disponibilidad",
        )
        if any(k in page_text for k in no_results_keywords):
            logger.info("No available slots found (explicit message)")
            return []

        if slots:
            return slots

        logger.debug("Could not parse results; raw snippet: %s", html[:500])
        return []

    def _map_cols(self, headers: list[str]) -> dict[str, int]:
        m: dict[str, int] = {}
        for i, h in enumerate(headers):
            if any(k in h for k in ("fecha", "día", "day")):
                m.setdefault("fecha", i)
            elif any(k in h for k in ("hora", "time", "horario")):
                m.setdefault("hora", i)
            elif any(k in h for k in ("médico", "doctor", "facultativo", "profesional")):
                m.setdefault("doctor", i)
            elif any(k in h for k in ("consulta", "sala", "room")):
                m.setdefault("consulta", i)
            elif any(k in h for k in ("centro", "lugar", "ubicación")):
                m.setdefault("centro", i)
        return m

    def _cells_to_slot(self, cells: list[str], col: dict[str, int]) -> "Slot | None":
        def get(key: str) -> str:
            idx = col.get(key)
            return cells[idx].strip() if idx is not None and idx < len(cells) else ""
        fecha = get("fecha")
        hora  = get("hora")
        if not fecha and not hora:
            return None
        # Try to split combined "fecha hora" cell
        if fecha and not hora:
            m = re.search(r"(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})\s+(\d{1,2}:\d{2})", fecha)
            if m:
                fecha, hora = m.group(1), m.group(2)
        return Slot(fecha=fecha, hora=hora,
                    doctor=get("doctor"), consulta=get("consulta"), centro=get("centro"))

    def _text_to_slot(self, text: str) -> "Slot | None":
        date_m = re.search(r"\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}", text)
        time_m = re.search(r"\d{1,2}:\d{2}", text)
        if not date_m:
            return None
        return Slot(fecha=date_m.group(), hora=time_m.group() if time_m else "",
                    doctor="", consulta="", centro="")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def get_especialidades_from_page(self) -> dict[str, str]:
        """Returns {value: label} dict of available specialties."""
        soup, _ = self._get_auto_page()
        if soup is None:
            return ESPECIALIDADES_DEFAULT
        sel = soup.find("select", {"name": "ddlEspecialidad"})
        if not sel:
            return ESPECIALIDADES_DEFAULT
        return {o["value"]: o.get_text(strip=True)
                for o in sel.find_all("option") if o.get_text(strip=True)}

    def get_medicos(self, compania: str, especialidad: str) -> dict[str, str]:
        """Returns {value: label} dict of available doctors for given filters."""
        _, fields = self._get_auto_page()
        if compania:
            fields["ddlCompania"] = compania
            _, fields = self._postback("ddlCompania", fields)
        if especialidad:
            fields["ddlEspecialidad"] = especialidad
            soup, fields = self._postback("ddlEspecialidad", fields)
            if soup:
                sel = soup.find("select", {"name": "ddlMedico"})
                if sel:
                    return {o["value"]: o.get_text(strip=True)
                            for o in sel.find_all("option") if o.get_text(strip=True)}
        return {"": "- Cualquier médico -"}

    def logout(self) -> None:
        try:
            self.session.get(f"{BASE_URL}/CerrarSesion.aspx", timeout=10)
        except requests.RequestException:
            pass
        self.session.cookies.clear()
        self._logged_in = False
