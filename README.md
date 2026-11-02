# Kocie jedzenie 🐱

Codzienne przypomnienie o kończącej się karmie dla kotów (GitHub Actions, `.github/workflows/karma.yml`).
Skrypt `script.py` czyta najnowsze potwierdzenie zamówienia Zooplus (IMAP), liczy datę
wyczerpania zapasów (300 g/dzień) oraz ostatni dzień na zamówienie (−4 dni robocze)
i wysyła maila (SMTP) 14 dni przed wyczerpaniem oraz w ostatnim dniu na zamówienie.

## Sekrety (Settings → Secrets and variables → Actions)

| Zmienna | Wymagana | Opis |
|---|---|---|
| `IMAP_SERVER` | tak | np. `imap.gmail.com` |
| `IMAP_PORT` | nie | domyślnie `993` |
| `SMTP_SERVER` | tak | np. `smtp.gmail.com` |
| `SMTP_PORT` | nie | `465` (SSL, domyślnie) lub `587` (STARTTLS) |
| `EMAIL_USER` | tak | login skrzynki (IMAP i SMTP) |
| `EMAIL_PASSWORD` | tak | hasło / hasło aplikacji — **Masked + Protected** |
| `NOTIFY_EMAIL` | nie | adresat przypomnień, domyślnie `EMAIL_USER` |
| `SMTP_USER` / `SMTP_PASSWORD` | nie | gdy SMTP ma inne dane logowania niż IMAP |

Opcjonalne (dopisz je w sekcji `env` w `karma.yml`, żeby zadziałały): `IMAP_FOLDER` (`INBOX`), `SENDER_FILTER` (`zooplus`), `IMAP_SEARCH_DAYS` (`180`),
`DAILY_GRAMS` (`300`), `DELIVERY_BUSINESS_DAYS` (`4`), `EARLY_REMINDER_DAYS` (`14`),
`TZ_NAME` (`Europe/Warsaw`), `DRY_RUN` (`true` = bez wysyłki), `TODAY_OVERRIDE` (`RRRR-MM-DD`, do testów).

## Harmonogram i testy

Workflow „Przypomnienie o karmie” uruchamia się codziennie (cron w `karma.yml`).
Ręcznie: Actions → Przypomnienie o karmie → Run workflow (`dry_run` = tylko log,
`today_override` = udawana data, np. dzień przypomnienia z logu).
Harmonogram działa tylko na domyślnej gałęzi repozytorium.
