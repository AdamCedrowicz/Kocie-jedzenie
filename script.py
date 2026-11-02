#!/usr/bin/env python3
"""
Powiadomienia o kończącej się karmie dla kotów.

Skrypt uruchamiany codziennie (GitHub Actions, .github/workflows/karma.yml):
  1. Loguje się przez IMAP i pobiera maile od Zooplus (potwierdzenia nadania paczek itp.).
  2. Składa z nich najnowsze zamówienie z karmą (FOOD_KEYWORD, domyślnie "Feringa"):
     datę zamówienia i łączną ilość karmy w gramach (sztuki x waga z nazwy produktu).
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
TIMEZONE = ZoneInfo(env("TZ_NAME", "Europe/Warsaw"))          # runnery GitHub Actions działają w UTC
SENDER_FILTER = env("SENDER_FILTER", "zooplus")               # fragment adresu nadawcy
SEARCH_DAYS = int(env("IMAP_SEARCH_DAYS", "180"))             # jak daleko wstecz szukać
FOOD_KEYWORD = env("FOOD_KEYWORD", "Feringa")                 # początek nazwy karmy w zamówieniu
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

# Waga jednej sztuki z nazwy produktu, np. "12 kg", "6 x 400 g", "2 x (6 x 400 g)", "1,5kg".
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

UNIT_TO_GRAMS = {"kg": 1000.0, "dag": 10.0, "g": 1.0, "gr": 1.0}

# Pozycja zamówienia w mailu Zooplus (tekst ze ściśniętymi białymi znakami):
#   "Feringa Classic Meat Menu, 6 x 400 g - Drób Drób Nr artykułu 1281422.7 8 x 44,96 PLN 359,68 PLN"
#   name – nazwa z wariantem (do "Nr artykułu"; bez "PLN", żeby nie wejść na poprzednią pozycję)
#   art  – numer artykułu (do usuwania duplikatów)
#   qty  – liczba zamówionych sztuk ("8 x 44,96 PLN")
ITEM_RE = re.compile(
    r"(?P<name>{kw}(?:(?!Nr\s+artyku|PLN).){{0,250}}?)"
    r"\s*Nr\s+artyku[łl]u:?\s*(?P<art>[\d.]+)"
    r"\s*(?P<qty>\d+)\s*[x×]\s*\d[\d\s.]*,\d{{2}}\s*(?:PLN|zł)".format(kw=re.escape(FOOD_KEYWORD)),
    re.IGNORECASE,
)

# Numer zamówienia w temacie ("... z zamówienia 454820177 ...") lub w treści ("Numer zamówienia: 454820177").
ORDER_NO_RE = re.compile(r"\b(\d{8,})\b")
BODY_ORDER_NO_RE = re.compile(r"Numer\s+zam[óo]wienia:?\s*(?:Data\s+zam[óo]wienia:?\s*)?(\d{8,})", re.IGNORECASE)

# Zooplus wysyła osobny mail dla każdej paczki: "Paczka 4 (z 4) z zamówienia ...".
PACKAGE_RE = re.compile(r"Paczka\s+(\d+)\s*\(z\s*\d+\)", re.IGNORECASE)
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


def extract_food_items(body: str) -> dict[str, float]:
    """Zwraca pozycje karmy z maila: {numer artykułu: gramy razem}.

    Liczone są tylko produkty, których nazwa zaczyna się od FOOD_KEYWORD (np. "Feringa"),
    więc przysmaki, żwirek czy zabawki z tego samego zamówienia są pomijane.
    Gramy = liczba sztuk ("8 x 44,96 PLN") * waga sztuki z nazwy ("6 x 400 g").
    """
    flat = re.sub(r"\s+", " ", body)
    items: dict[str, float] = {}
    for m in ITEM_RE.finditer(flat):
        weights = [
            int(w.group("outer") or 1)
            * int(w.group("inner") or 1)
            * float(w.group("amount").replace(",", "."))
            * UNIT_TO_GRAMS[w.group("unit").lower()]
            for w in WEIGHT_RE.finditer(m.group("name"))
        ]
        if not weights:
            log.warning("Pozycja bez wagi w nazwie, pominięta: %r", m.group("name"))
            continue
        # Kilka wag w nazwie to zwykle opis tego samego produktu ("12 kg (2 x 6 kg)") – bierzemy największą.
        grams = int(m.group("qty")) * max(weights)
        log.info("Pozycja: %r -> %s szt. = %.0f g", m.group("name").strip(), m.group("qty"), grams)
        items[m.group("art")] = items.get(m.group("art"), 0) + grams
    return items


# --------------------------------------------------------------------------- #
# IMAP
# --------------------------------------------------------------------------- #

@dataclass
class Order:
    number: str
    order_date: date
    grams: float


def fetch_zooplus_messages() -> list[Message]:
    """Pobiera maile od Zooplus z ostatnich SEARCH_DAYS dni (maks. 50 najnowszych)."""
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

        messages = []
        for uid in uids[-50:]:
            status, msg_data = imap.uid("fetch", uid, "(RFC822)")
            if status == "OK" and msg_data and msg_data[0] is not None:
                messages.append(email.message_from_bytes(msg_data[0][1]))
    return messages


def latest_food_order(messages: list[Message]) -> Order:
    """Składa zamówienia z maili i zwraca najnowsze, w którym była karma.

    Jedno zamówienie to często kilka maili (po jednym na paczkę), każdy z częścią produktów.
    Pozycje grupujemy po numerze zamówienia i numerze paczki, a w obrębie paczki po numerze
    artykułu, więc ponownie wysłany mail tej samej paczki nie liczy się podwójnie.
    Jeśli jest mail z całym zamówieniem (bez numeru paczki), używamy tylko jego.
    """
    # numer zamówienia -> {"date": data, "parts": {numer paczki lub "całość": {artykuł: gramy}}}
    orders: dict[str, dict] = {}
    for msg in messages:
        subject = decode_str(msg.get("Subject"))
        if not ORDER_SUBJECT_RE.search(subject):
            continue
        body = message_body_text(msg)
        items = extract_food_items(body)
        if not items:
            continue
        number_match = ORDER_NO_RE.search(subject) or BODY_ORDER_NO_RE.search(body)
        order_date = extract_order_date(body, msg)
        if not number_match or not order_date:
            log.warning("Pominięto mail %r – brak numeru lub daty zamówienia.", subject)
            continue
        package = PACKAGE_RE.search(subject)
        order = orders.setdefault(number_match.group(1), {"date": order_date, "parts": {}})
        order["parts"].setdefault(package.group(1) if package else "całość", {}).update(items)
        log.info("Mail %r: %d pozycji karmy.", subject, len(items))

    if not orders:
        raise RuntimeError(f"Nie znaleziono maila Zooplus z zamówieniem karmy '{FOOD_KEYWORD}'.")

    number, order = max(orders.items(), key=lambda kv: (kv[1]["date"], kv[0]))
    parts = order["parts"]
    if "całość" in parts:
        parts = {"całość": parts["całość"]}
    grams = sum(sum(part.values()) for part in parts.values())
    log.info("Najnowsze zamówienie karmy: %s z %s, %.0f g.", number, order["date"], grams)
    return Order(number, order["date"], grams)


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

    order = latest_food_order(fetch_zooplus_messages())
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

    if runs_out < today:
        log.warning("Data wyczerpania zapasów już minęła – czy ostatnie zamówienie karmy jest w skrzynce?")
    if not reasons:
        log.info("Dziś nie ma powodu do wysyłania przypomnienia.")
        return 0

    subject = (
        "🐱 PILNE: ostatni dzień na zamówienie karmy" if today == last_order_day
        else "🐱 Przypomnienie: karma dla kotów kończy się za 2 tygodnie"
    )
    body = "\n\n".join(reasons) + (
        "\n\nSzczegóły:\n"
        f"- ostatnie zamówienie karmy: nr {order.number} z {order.order_date:%d.%m.%Y}\n"
        f"- zamówiona ilość {FOOD_KEYWORD}: {order.grams / 1000:.2f} kg\n"
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
