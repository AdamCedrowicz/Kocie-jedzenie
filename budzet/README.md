# Budżet – analiza wyciągów bankowych

Etap 2 (gotowy): wczytanie wyciągów z PKO (konto, karta kredytowa – CSV lub miesięczne zestawienie PDF),
mBanku i Erste do jednej tabeli.

```
pip install -r budzet/requirements.txt
cp budzet/ustawienia.example.json budzet/ustawienia.json   # wpisz swoje numery rachunków
python -m budzet FOLDER_Z_CSV --wynik transakcje.xlsx                          # tylko Etap 2
python -m budzet FOLDER_Z_CSV --budzet budzet_domowy.xlsx --wynik transakcje.xlsx  # + kategorie
python -m unittest discover -s budzet/tests -t .
```

Format pliku rozpoznawany jest po treści (nazwa pliku nie ma znaczenia). Kontrole przy wczytaniu:
PKO konto i Erste – ciągłość sald, mBank – sumy wpływów/wydatków z nagłówka, Erste – liczba operacji,
PDF karty – podsumowanie zestawienia (saldo, operacje, wpłaty, zadłużenie).

Kolumny tabeli: `id, zrodlo, rachunek, data` (data zakupu), `data_ksiegowania, kwota` (< 0 = wydatek),
`waluta, kontrahent, rachunek_kontrahenta, tytul, typ, kategoria_banku` (tylko mBank), `saldo, opis, plik,
transfer_wewnetrzny, transfer_para`.

- **Duplikaty**: nakładające się eksporty można wrzucać bez obaw – ta sama operacja ma ten sam `id`.
- **Transfery wewnętrzne** (nie liczą się do budżetu): przelew na/z rachunku z `moje_rachunki`, numer własnego
  rachunku w tytule (np. AUTOPAY), kontrahent z `kontrahenci_wewnetrzni` (kantor), albo druga strona takiego
  przelewu w innym banku (ta sama kwota, przeciwny znak, do 3 dni).

Wyciągi (`*.csv`, `*.pdf`), wyniki (`*.xlsx`), `ustawienia.json`, `reguly_prywatne.csv` i `etykiety.csv` są w `.gitignore`.

## Etap 3: kategoryzacja

Źródła kategorii, pierwsze wygrywa: **transfer** → **ręcznie** (`etykiety.csv`) → **reguła** (`reguly_prywatne.csv`,
potem `reguly.csv`) → **budżet** (kwota z Twojej siatki trafiła w tę transakcję: ta sama kwota, ±2 dni) →
**model** (TF-IDF na kontrahencie/tytule + regresja logistyczna, uczony na wszystkim powyżej).
Model z pewnością < 0,6 ląduje w arkuszu „Do sprawdzenia”.

- `--budzet` to eksport „Budżet domowy” do .xlsx: z niego lista kategorii (klucz `Grupa › Nazwa`, bo nazwy się
  powtarzają) i historyczne kwoty do nauki.
- Reguły: CSV `kategoria;kontrahent;tytul;opis;typ;kierunek;projekt;uwagi`. Warunki to wyrażenia regularne
  bez rozróżniania wielkości liter (`LIDL|BIEDRONKA`, `^ORLEN`); `kierunek` `+`/`-`; kategoria jako nazwa
  (jeśli jednoznaczna), `Grupa › Nazwa` albo `POZA BUDŻETEM`. `projekt` (np. Mikołowska) użyje Etap 4.
- Poprawki: w wyniku popraw kolumnę `kategoria` w arkuszu „Do sprawdzenia” i podaj ten plik jako
  `--etykiety`, albo dopisz `id;kategoria` do `etykiety.csv`. Model uczy się ich przy kolejnym uruchomieniu.
- Raport w konsoli: pokrycie reguł, zgodność reguł z budżetem (szczegóły w arkuszu „Reguła vs budżet”),
  trafność modelu na odłożonym okresie.
