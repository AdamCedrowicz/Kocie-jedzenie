# Budżet – analiza wyciągów bankowych

Etap 2 (gotowy): wczytanie wyciągów z PKO (konto, karta kredytowa – CSV lub miesięczne zestawienie PDF),
mBanku i Erste do jednej tabeli.

```
pip install -r budzet/requirements.txt
cp budzet/ustawienia.example.json budzet/ustawienia.json   # wpisz swoje numery rachunków
python -m budzet FOLDER_Z_CSV --wynik transakcje.xlsx
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

Wyciągi (`*.csv`, `*.pdf`), wyniki (`*.xlsx`) i `ustawienia.json` są w `.gitignore`.
