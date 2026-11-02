#!/usr/bin/env python3
"""
Powiadomienia o kończącej się karmie dla kotów.

Skrypt uruchamiany codziennie (GitLab Scheduled Pipeline):
  1. Loguje się przez IMAP i szuka najnowszego potwierdzenia zamówienia od Zooplus.
  2. Wyciąga z treści maila datę zamówienia i łączną ilość karmy (w gramach).
  3. Liczy datę wyczerpania zapasów oraz ostatni dzień na zamówienie
     (data wyczerpania minus N dni roboczych, z pominięciem weekendów).
  4. Wysyła przypomnienie przez SMTP tylko, gdy dziś jest:
       - dokładnie 14 dni przed wyczerpaniem zapasów, lub
       - ostatni możliwy dzień na zamówienie.

Wszystkie dane wrażliwe są czytane ze zmiennych środowiskowych.
"""

import email
import imaplib
import logging
import os
import re
import smtplib
import ssl
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kocie-jedzenie")


# --------------------------------------------------------------------------- #
# Konfiguracja
# --------------------------------------------------------------------------- #

def env(name: str, default: str | None = None, required: bool = False) -> str:
    """Pobiera zmienną środowiskową; przerywa działanie, gdy brakuje wymaganej."""
    value = os.environ.get(name, default)
    if required and not value:
        log.error("Brak wymaganej zmiennej środowiskowej: %s", name)
        sys.exit(2)
    return value


DAILY_GRAMS = int(env("DAILY_GRAMS", "300"))                 # dzienne spożycie
DELIVERY_BUSINESS_DAYS = int(env("DELIVERY_BUSINESS_DAYS", "4"))
EARLY_REMINDER_DAYS = int(env("EARLY_REMINDER_DAYS", "14"))   # "dwa tygodnie przed"
TIMEZONE = ZoneInfo(env("TZ_NAME", "Europe/Warsaw"))          # runnery GitLaba działają w UTC
SENDER_FILTER = env("SENDER_FILTER", "zooplus")               # fragment adresu nadawcy
SEARCH_DAYS = int(env("IMAP_SEARCH_DAYS", "180"))             # jak daleko wstecz szukać
DRY_RUN = env("DRY_RUN", "false").lower() in ("1", "true", "yes")

# Temat maila, który uznajemy za potwierdzenie zamówienia (PL + EN).
ORDER_SUBJECT_RE = re.compile(
    r"zam[óo]wieni|potwierdzeni|order|bestellung", re.IGNORECASE
)


# --------------------------------------------------------------------------- #
# Pomocnicze: dekodowanie maila
# --------------------------------------------------------------------------- #

class _HTMLToText(HTMLParser):
    """Minimalny konwerter HTML -> tekst (każdy blok/komórka tabeli w nowej linii)."""

    BLOCK_TAGS = {"br", "p", "div", "tr", "td", "th", "li", "h1", "h2", "h3", "h4", "table"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head"):
            self._skip += 1
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head") and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        return unescape("".join(self.parts))


def html_to_text(html: str) -> str:
    parser = _HTMLToText()
    parser.feed(html)
    return parser.text()


def decode_str(value: str | None) -> str:
    """Dekoduje nagłówki typu =?utf-8?Q?...?=."""
    return str(make_header(decode_header(value))) if value else ""


def message_body_text(msg: Message) -> str:
    """Zwraca treść maila jako tekst: preferuje text/plain, w razie braku konwertuje HTML.

    Bierzemy tylko JEDNĄ wersję treści, żeby nie policzyć karmy podwójnie
    (maile często mają tę samą treść w text/plain i text/html).
    """
    plain, html = None, None
    for part in msg.walk():
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        text = payload.decode(charset, errors="replace")
        ctype = part.get_content_type()
        if ctype == "text/plain" and plain is None:
            plain = text
        elif ctype == "text/html" and html is None:
            html = text
    if plain and plain.strip():
        return plain
    return html_to_text(html) if html else ""


# --------------------------------------------------------------------------- #
# Ekstrakcja danych (regex)
# --------------------------------------------------------------------------- #

PL_MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
    "lipca": 7, "sierpnia": 8, "września": 9, "wrzesnia": 9, "października": 10,
    "pazdziernika": 10, "listopada": 11, "grudnia": 12,
}

# Data po etykiecie, np. "Data zamówienia: 12.10.2026" / "Order date: 2026-10-12".
LABELLED_DATE_RE = re.compile(
    r"(?:data\s+zam[óo]wienia|zam[óo]wienie\s+z\s+dnia|z\s+dnia|order\s+date|date\s+of\s+order)"
    r"\s*:?\s*(?P<date>\d{1,2}[./-]\d{1,2}[./-]\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}\s+[a-ząćęłńóśźż]+\s+\d{4})",
    re.IGNORECASE,
)

# Ilość karmy, np. "12 kg", "2 x 12 kg", "24 x 85 g", "2 x (6 x 400 g)", "1,5kg".
#   outer  – opcjonalny mnożnik zewnętrzny (liczba opakowań zbiorczych)
#   inner  – opcjonalny mnożnik wewnętrzny (liczba sztuk w opakowaniu)
#   amount – waga jednej sztuki
#   unit   – jednostka (kg / g / gr / dag)
WEIGHT_RE = re.compile(
    r"(?:(?P<outer>\d+)\s*[x×]\s*\(?\s*)?"
    r"(?:(?P<inner>\d+)\s*[x×]\s*)?"
    r"(?P<amount>\d+(?:[.,]\d+)?)\s*(?P<unit>kg|dag|gr|g)\b",
    re.IGNORECASE,
)

# Liczba sztuk podana w osobnej kolumnie/linii, np. "Ilość: 2" lub "2 szt."
QTY_RE = re.compile(r"(?:ilo[śs][ćc]|quantity|menge)\s*:?\s*(?P<qty>\d+)|(?P<qty2>\d+)\s*szt", re.IGNORECASE)

# Linie, w których waga nie dotyczy karmy (np. waga paczki, darmowa dostawa od X kg).
IGNORE_LINE_RE = re.compile(r"przesy[łl]k|paczk|wysy[łl]k|dostaw|shipping|parcel|gratis", re.IGNORECASE)

UNIT_TO_GRAMS = {"kg": 1000.0, "dag": 10.0, "g": 1.0, "gr": 1.0}


def parse_date_string(raw: str) -> date | None:
    """Zamienia różne formaty dat na obiekt date."""
    raw = raw.strip()
    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    m = re.match(r"(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(\d{4})", raw, re.IGNORECASE)
    if m and m.group(2).lower() in PL_MONTHS:
        return date(int(m.group(3)), PL_MONTHS[m.group(2).lower()], int(m.group(1)))
    return None


def extract_order_date(body: str, msg: Message) -> date | None:
    """Data zamówienia z treści; w razie braku – data wysłania maila (nagłówek Date)."""
    m = LABELLED_DATE_RE.search(body)
    if m:
        parsed = parse_date_string(m.group("date"))
        if parsed:
            return parsed
    if msg.get("Date"):
        try:
            return parsedate_to_datetime(msg["Date"]).astimezone(TIMEZONE).date()
        except (TypeError, ValueError):
            pass
    return None


def extract_total_grams(body: str) -> float:
    """Sumuje łączną ilość karmy w gramach ze wszystkich pozycji zamówienia.

    Dla każdej linii z wagą liczy: outer * inner * amount * przelicznik_jednostki.
    Jeśli liczba sztuk jest podana osobno ("Ilość: 2"), w tej samej lub jednej
    z dwóch kolejnych linii, waga jest dodatkowo przez nią mnożona.
    """
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    total = 0.0
    for i, line in enumerate(lines):
        if IGNORE_LINE_RE.search(line):
            continue
        matches = list(WEIGHT_RE.finditer(line))
        if not matches:
            continue
        # Kilka wag w jednej linii to zwykle opis tego samego produktu
        # (np. "12 kg (2 x 6 kg)") – bierzemy największą wartość.
        line_grams = max(
            int(m.group("outer") or 1)
            * int(m.group("inner") or 1)
            * float(m.group("amount").replace(",", "."))
            * UNIT_TO_GRAMS[m.group("unit").lower()]
            for m in matches
        )
        # Szukamy osobno podanej liczby sztuk w tej i dwóch kolejnych liniach.
        qty = 1
        for j, nearby in enumerate(lines[i:i + 3]):
            if j > 0 and WEIGHT_RE.search(nearby):
                break  # kolejny produkt – nie podbieramy jego ilości
            q = QTY_RE.search(nearby)
            if q:
                qty = int(q.group("qty") or q.group("qty2"))
                break
        log.info("Pozycja: %r -> %d x %.0f g", line, qty, line_grams)
        total += qty * line_grams
    return total


# --------------------------------------------------------------------------- #
# IMAP
# --------------------------------------------------------------------------- #

@dataclass
class Order:
    order_date: date
    grams: float
    subject: str


def fetch_latest_order() -> Order:
    """Znajduje najnowszy mail od Zooplus z potwierdzeniem zamówienia i go parsuje."""
    host = env("IMAP_SERVER", required=True)
    port = int(env("IMAP_PORT", "993"))
    user = env("EMAIL_USER", required=True)
    password = env("EMAIL_PASSWORD", required=True)
    folder = env("IMAP_FOLDER", "INBOX")

    since = (datetime.now(TIMEZONE) - timedelta(days=SEARCH_DAYS)).strftime("%d-%b-%Y")

    with imaplib.IMAP4_SSL(host, port) as imap:
        imap.login(user, password)
        imap.select(f'"{folder}"', readonly=True)  # tylko odczyt – nie oznaczamy maili jako przeczytane

        status, data = imap.uid("search", None, f'(FROM "{SENDER_FILTER}" SINCE {since})')
        if status != "OK":
            raise RuntimeError(f"Wyszukiwanie IMAP nie powiodło się: {status}")
        uids = data[0].split()
        log.info("Znaleziono %d maili od '%s' z ostatnich %d dni.", len(uids), SENDER_FILTER, SEARCH_DAYS)

        # Pobieramy wiadomości i sortujemy po dacie (najnowsze najpierw);
        # kolejność UID nie zawsze odpowiada dacie wysłania.
        messages: list[tuple[datetime, Message]] = []
        for uid in uids[-50:]:
            status, msg_data = imap.uid("fetch", uid, "(RFC822)")
            if status != "OK" or not msg_data or msg_data[0] is None:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            try:
                sent = parsedate_to_datetime(msg["Date"])
            except (TypeError, ValueError):
                sent = datetime.min.replace(tzinfo=TIMEZONE)
            if sent.tzinfo is None:
                sent = sent.replace(tzinfo=TIMEZONE)
            messages.append((sent, msg))

    messages.sort(key=lambda pair: pair[0], reverse=True)

    # Pierwszy (najnowszy) mail, który wygląda na zamówienie i zawiera ilość karmy.
    for _, msg in messages:
        subject = decode_str(msg.get("Subject"))
        if not ORDER_SUBJECT_RE.search(subject):
            continue
        body = message_body_text(msg)
        grams = extract_total_grams(body)
        order_date = extract_order_date(body, msg)
        if grams > 0 and order_date:
            log.info("Użyto maila: %r (data zamówienia %s, %.0f g)", subject, order_date, grams)
            return Order(order_date, grams, subject)
        log.warning("Pominięto mail %r – nie udało się odczytać daty lub ilości karmy.", subject)

    raise RuntimeError("Nie znaleziono maila z potwierdzeniem zamówienia Zooplus.")


# --------------------------------------------------------------------------- #
# Obliczenia
# --------------------------------------------------------------------------- #

def subtract_business_days(start: date, business_days: int) -> date:
    """Cofa się o podaną liczbę dni roboczych (pn–pt), pomijając soboty i niedziele."""
    current = start
    remaining = business_days
    while remaining > 0:
        current -= timedelta(days=1)
        if current.weekday() < 5:  # 0=pon ... 4=pt
            remaining -= 1
    return current


def depletion_date(order_date: date, grams: float) -> date:
    """Data wyczerpania = data zamówienia + pełne dni, na które wystarczy karmy.

    Zaokrąglamy w dół (int), bo niepełny dzień oznacza, że karmy już zabraknie.
    """
    return order_date + timedelta(days=int(grams // DAILY_GRAMS))


# --------------------------------------------------------------------------- #
# SMTP
# --------------------------------------------------------------------------- #

def send_email(subject: str, body: str) -> None:
    host = env("SMTP_SERVER", required=True)
    port = int(env("SMTP_PORT", "465"))
    user = env("EMAIL_USER", required=True)
    password = env("EMAIL_PASSWORD", required=True)
    sender = env("SMTP_USER", user)
    recipient = env("NOTIFY_EMAIL", user)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(body)

    if DRY_RUN:
        log.info("DRY_RUN – mail NIE został wysłany:\n%s\n%s", subject, body)
        return

    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=context) as smtp:
            smtp.login(env("SMTP_USER", user), env("SMTP_PASSWORD", password))
            smtp.send_message(msg)
    else:  # np. 587 – STARTTLS
        with smtplib.SMTP(host, port) as smtp:
            smtp.starttls(context=context)
            smtp.login(env("SMTP_USER", user), env("SMTP_PASSWORD", password))
            smtp.send_message(msg)
    log.info("Wysłano przypomnienie do %s.", recipient)


# --------------------------------------------------------------------------- #
# Główna logika
# --------------------------------------------------------------------------- #

def main() -> int:
    # TODAY_OVERRIDE (RRRR-MM-DD) pozwala przetestować wysyłkę dla wybranego dnia.
    override = os.environ.get("TODAY_OVERRIDE")
    today = date.fromisoformat(override) if override else datetime.now(TIMEZONE).date()

    order = fetch_latest_order()
    runs_out = depletion_date(order.order_date, order.grams)
    last_order_day = subtract_business_days(runs_out, DELIVERY_BUSINESS_DAYS)
    early_reminder_day = runs_out - timedelta(days=EARLY_REMINDER_DAYS)

    log.info("Dzisiaj:                  %s", today)
    log.info("Data zamówienia:          %s", order.order_date)
    log.info("Ilość karmy:              %.0f g (%.1f dni)", order.grams, order.grams / DAILY_GRAMS)
    log.info("Wyczerpanie zapasów:      %s", runs_out)
    log.info("Przypomnienie 14 dni:     %s", early_reminder_day)
    log.info("Ostatni dzień zamówienia: %s", last_order_day)

    reasons = []
    if today == early_reminder_day:
        reasons.append(f"Za {EARLY_REMINDER_DAYS} dni ({runs_out:%d.%m.%Y}) skończy się karma dla kotów.")
    if today == last_order_day:
        reasons.append(
            "DZIŚ jest ostatni dzień na zamówienie karmy! "
            f"Dostawa trwa ok. {DELIVERY_BUSINESS_DAYS} dni robocze, a karma skończy się {runs_out:%d.%m.%Y}."
        )

    if not reasons:
        log.info("Dziś nie ma powodu do wysyłania przypomnienia.")
        return 0

    subject = (
        "🐱 PILNE: ostatni dzień na zamówienie karmy" if today == last_order_day
        else "🐱 Przypomnienie: karma dla kotów kończy się za 2 tygodnie"
    )
    body = "\n\n".join(reasons) + (
        "\n\nSzczegóły:\n"
        f"- ostatnie zamówienie: {order.order_date:%d.%m.%Y} ({order.subject})\n"
        f"- zamówiona ilość: {order.grams / 1000:.2f} kg\n"
        f"- dzienne spożycie: {DAILY_GRAMS} g\n"
        f"- szacowane wyczerpanie zapasów: {runs_out:%d.%m.%Y}\n"
        f"- ostatni dzień na zamówienie: {last_order_day:%d.%m.%Y}\n"
    )
    send_email(subject, body)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log.exception("Skrypt zakończył się błędem.")
        sys.exit(1)
