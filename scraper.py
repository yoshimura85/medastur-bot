"""Scraper for paciente.medastur.com — handles ASP.NET ViewState sessions."""
import re
import logging
from dataclasses import dataclass
from typing import Optional

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://paciente.medastur.com"
LOGIN_URL = f"{BASE_URL}/Login.aspx"
HOME_URL = f"{BASE_URL}/Inicio.aspx"
APPOINTMENTS_PATHS = [
    "/CitaPrevia/Citas.aspx",
    "/MisCitas.aspx",
    "/Citas.aspx",
    "/CitaPrevia.aspx",
]

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-ES,es;q=0.9",
}


@dataclass
class Appointment:
    doctor: str
    specialty: str
    date: str
    time: str
    location: str

    def key(self) -> str:
        return f"{self.doctor}|{self.date}|{self.time}"

    def to_dict(self) -> dict:
        return {
            "doctor": self.doctor,
            "specialty": self.specialty,
            "date": self.date,
            "time": self.time,
            "location": self.location,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Appointment":
        return cls(**d)

    def __str__(self) -> str:
        lines = [f"👨‍⚕️ {self.doctor}"]
        if self.specialty:
            lines.append(f"🏥 {self.specialty}")
        lines.append(f"📅 {self.date}  🕐 {self.time}")
        if self.location:
            lines.append(f"📍 {self.location}")
        return "\n".join(lines)


class MedasturScraper:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._logged_in = False

    def _get_aspnet_fields(self, html: str) -> dict:
        soup = BeautifulSoup(html, "html.parser")
        fields = {}
        for name in ("__VIEWSTATE", "__EVENTVALIDATION", "__VIEWSTATEGENERATOR",
                     "__EVENTTARGET", "__EVENTARGUMENT"):
            tag = soup.find("input", {"name": name})
            if tag:
                fields[name] = tag.get("value", "")
        return fields

    def _discover_login_fields(self, html: str) -> dict[str, str]:
        """Try to find the username/password input names dynamically."""
        soup = BeautifulSoup(html, "html.parser")
        field_map: dict[str, str] = {}

        for inp in soup.find_all("input"):
            name = inp.get("name", "")
            itype = inp.get("type", "text").lower()
            iid = inp.get("id", "").lower()
            label = (inp.get("placeholder", "") + iid).lower()

            if itype == "password":
                field_map["password_field"] = name
            elif itype in ("text", "email") and any(
                k in label for k in ("user", "login", "nif", "dni", "usuario", "email", "correo")
            ):
                field_map["username_field"] = name

        # fallback: first text input = username
        if "username_field" not in field_map:
            for inp in soup.find_all("input", {"type": ["text", "email"]}):
                name = inp.get("name", "")
                if name and not name.startswith("__"):
                    field_map["username_field"] = name
                    break

        return field_map

    def _find_submit_button(self, html: str) -> dict[str, str]:
        soup = BeautifulSoup(html, "html.parser")
        for btn in soup.find_all(["input", "button"]):
            btype = btn.get("type", "").lower()
            bval = (btn.get("value", "") + btn.get("id", "")).lower()
            if btype == "submit" or any(k in bval for k in ("enviar", "login", "entrar", "acceder")):
                name = btn.get("name", "")
                value = btn.get("value", "")
                if name:
                    return {name: value}
        return {}

    def login(self, username: str, password: str) -> bool:
        try:
            resp = self.session.get(LOGIN_URL, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error("Error fetching login page: %s", e)
            return False

        hidden = self._get_aspnet_fields(resp.text)
        fields = self._discover_login_fields(resp.text)
        submit = self._find_submit_button(resp.text)

        if not fields.get("username_field") or not fields.get("password_field"):
            logger.error("Could not detect login form fields")
            logger.debug("Login page snippet: %s", resp.text[:2000])
            return False

        payload = {
            fields["username_field"]: username,
            fields["password_field"]: password,
            **hidden,
            **submit,
        }

        try:
            resp2 = self.session.post(LOGIN_URL, data=payload, timeout=20)
            resp2.raise_for_status()
        except requests.RequestException as e:
            logger.error("Error posting login: %s", e)
            return False

        # Detect failed login: still on login page or error message
        if self._is_login_page(resp2.url) and self._has_login_error(resp2.text):
            logger.warning("Login failed: bad credentials or error on page")
            return False

        self._logged_in = True
        logger.info("Login successful for user %s", username)
        return True

    def _is_login_page(self, url: str) -> bool:
        return "login" in url.lower() or "inicio.aspx" in url.lower()

    def _has_login_error(self, html: str) -> bool:
        lower = html.lower()
        return any(k in lower for k in (
            "contraseña incorrecta", "usuario incorrecto", "error",
            "credenciales", "invalid", "no válido", "acceso denegado"
        ))

    def get_appointments(self) -> list[Appointment]:
        if not self._logged_in:
            raise RuntimeError("Not logged in")

        for path in APPOINTMENTS_PATHS:
            url = BASE_URL + path
            try:
                resp = self.session.get(url, timeout=20)
                if resp.status_code == 200 and not self._is_login_page(resp.url):
                    appointments = self._parse_appointments(resp.text)
                    if appointments is not None:
                        logger.info("Found appointments page at %s (%d items)", url, len(appointments))
                        return appointments
            except requests.RequestException as e:
                logger.warning("Error fetching %s: %s", url, e)

        # If specific paths fail, scan the home page for links
        return self._scan_from_home()

    def _scan_from_home(self) -> list[Appointment]:
        try:
            resp = self.session.get(HOME_URL, timeout=20)
            soup = BeautifulSoup(resp.text, "html.parser")
            for link in soup.find_all("a", href=True):
                href = link["href"]
                text = link.get_text(strip=True).lower()
                if any(k in text + href.lower() for k in ("cita", "consulta", "appointment")):
                    full_url = href if href.startswith("http") else BASE_URL + "/" + href.lstrip("/")
                    try:
                        r = self.session.get(full_url, timeout=20)
                        result = self._parse_appointments(r.text)
                        if result is not None:
                            return result
                    except requests.RequestException:
                        pass
        except requests.RequestException as e:
            logger.error("Error scanning home: %s", e)
        return []

    def _parse_appointments(self, html: str) -> list[Appointment] | None:
        soup = BeautifulSoup(html, "html.parser")
        appointments: list[Appointment] = []

        # Strategy 1: look for table rows with appointment-like data
        tables = soup.find_all("table")
        for table in tables:
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue

            headers = [th.get_text(strip=True).lower() for th in rows[0].find_all(["th", "td"])]
            if not any(k in " ".join(headers) for k in ("fecha", "médico", "doctor", "doctor", "especialidad", "cita")):
                continue

            col = self._map_columns(headers)
            for row in rows[1:]:
                cells = [td.get_text(strip=True) for td in row.find_all("td")]
                if not cells or all(c == "" for c in cells):
                    continue
                appt = self._cells_to_appointment(cells, col)
                if appt:
                    appointments.append(appt)

        if appointments:
            return appointments

        # Strategy 2: look for div-based cards
        for div in soup.find_all("div", class_=re.compile(r"cita|appointment|consulta|card", re.I)):
            text = div.get_text(separator="\n", strip=True)
            appt = self._text_to_appointment(text)
            if appt:
                appointments.append(appt)

        if appointments:
            return appointments

        # Return empty list (valid empty result) if the page looks like an appointments page
        page_text = soup.get_text(strip=True).lower()
        if any(k in page_text for k in ("no tiene citas", "sin citas", "no hay citas",
                                         "no existen citas", "no appointments")):
            return []

        # Can't determine if this is the right page
        return None

    def _map_columns(self, headers: list[str]) -> dict[str, int]:
        mapping: dict[str, int] = {}
        for i, h in enumerate(headers):
            if any(k in h for k in ("fecha", "day", "día")):
                mapping.setdefault("date", i)
            elif any(k in h for k in ("hora", "time", "horario")):
                mapping.setdefault("time", i)
            elif any(k in h for k in ("médico", "doctor", "profesional", "nombre")):
                mapping.setdefault("doctor", i)
            elif any(k in h for k in ("especialidad", "servicio", "tipo")):
                mapping.setdefault("specialty", i)
            elif any(k in h for k in ("centro", "lugar", "ubicación", "consultorio")):
                mapping.setdefault("location", i)
        return mapping

    def _cells_to_appointment(self, cells: list[str], col: dict[str, int]) -> Appointment | None:
        def get(key: str) -> str:
            idx = col.get(key)
            return cells[idx] if idx is not None and idx < len(cells) else ""

        date = get("date")
        if not date:
            return None

        return Appointment(
            doctor=get("doctor"),
            specialty=get("specialty"),
            date=date,
            time=get("time"),
            location=get("location"),
        )

    def _text_to_appointment(self, text: str) -> Appointment | None:
        date_match = re.search(r"\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}", text)
        time_match = re.search(r"\d{1,2}:\d{2}", text)
        if not date_match:
            return None
        return Appointment(
            doctor="",
            specialty="",
            date=date_match.group(),
            time=time_match.group() if time_match else "",
            location="",
        )

    def logout(self) -> None:
        try:
            self.session.get(f"{BASE_URL}/Logout.aspx", timeout=10)
        except requests.RequestException:
            pass
        self.session.cookies.clear()
        self._logged_in = False
