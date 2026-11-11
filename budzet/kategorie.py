"""Kategorie i historyczne etykiety z arkusza "Budżet domowy" (eksport do .xlsx).

Arkusz miesięczny: kolumna A = osoba (1 Adam, 2 Hania, 3 wspólne), B = nazwa kategorii,
kolumny I..AM = dni 1..31, komórka = kwota albo formuła "=10+5,20". Wiersz z pustym A
i tekstem w B to nagłówek grupy. Nazwy się powtarzają, więc klucz kategorii to "Grupa › Nazwa".
"""
import re
from datetime import date

import openpyxl
import pandas as pd

MIESIACE = ["Styczeń", "Luty", "Marzec", "Kwiecień", "Maj", "Czerwiec", "Lipiec", "Sierpień", "Wrzesień",
            "Październik", "Listopad", "Grudzień"]
PIERWSZY_WIERSZ, OSTATNI_WIERSZ, WIERSZ_WYDATKOW = 58, 257, 74  # powyżej 74 = przychody
KOL_DZIEN_1 = 9  # kolumna I


def _kategorie(ws):
    grupa, out = "Przychody", []
    for r in range(PIERWSZY_WIERSZ, OSTATNI_WIERSZ + 1):
        a, b = ws.cell(r, 1).value, str(ws.cell(r, 2).value or "").strip()
        if b in ("", ".", "Wydatki", "Kategoria", "SUMA:"):
            continue
        if a is None:
            grupa = b
            continue
        out.append({"wiersz": r, "grupa": grupa, "nazwa": b, "klucz": f"{grupa} › {b}",
                    "przychod": r < WIERSZ_WYDATKOW})
    return pd.DataFrame(out)


def wczytaj_kategorie(xlsx, arkusz="Styczeń"):
    return _kategorie(openpyxl.load_workbook(xlsx, data_only=True)[arkusz])


def _kwoty(v):
    """15.5 -> [15.5]; '=10+5,20' -> [10, 5.2]; tekst/puste -> []."""
    if isinstance(v, (int, float)):
        return [float(v)] if v else []
    s = str(v or "").strip()
    if not s.startswith("="):
        return []
    s = s[1:].replace(",", ".").replace(" ", "")
    if not re.fullmatch(r"[-+]?\d+(\.\d+)?([-+]\d+(\.\d+)?)*", s):
        return []  # formuły SUM/IF itp.
    return [float(x) for x in re.findall(r"[-+]?\d+(?:\.\d+)?", s)]


def etykiety_z_budzetu(xlsx, rok):
    """Każda ręcznie wpisana kwota z arkuszy miesięcznych: (data, kwota ze znakiem, klucz)."""
    formuly, wartosci = openpyxl.load_workbook(xlsx), openpyxl.load_workbook(xlsx, data_only=True)
    out = []
    for mies, arkusz in enumerate(MIESIACE, start=1):
        if arkusz not in formuly.sheetnames:
            continue
        ws = formuly[arkusz]
        for k in _kategorie(wartosci[arkusz]).itertuples():
            for dzien in range(1, 32):
                for a in _kwoty(ws.cell(k.wiersz, KOL_DZIEN_1 + dzien - 1).value):
                    try:
                        d = date(rok, mies, dzien)
                    except ValueError:
                        continue
                    out.append({"data": pd.Timestamp(d), "kwota": a if k.przychod else -a, "klucz": k.klucz})
    return pd.DataFrame(out, columns=["data", "kwota", "klucz"])


def dopasuj_do_transakcji(trans, etykiety, dni=2):
    """Dla każdej kwoty z budżetu szuka transakcji o tej samej kwocie (ze znakiem) do `dni` dni obok.
    Zwraca Series: id transakcji -> klucz kategorii. Każda transakcja i każda kwota użyte raz."""
    t = trans[~trans.transfer_wewnetrzny]
    po_kwocie = {k: g for k, g in t.groupby(t.kwota.round(2))}
    wynik = {}
    for e in etykiety.sort_values("data").itertuples():
        g = po_kwocie.get(round(e.kwota, 2))
        if g is None:
            continue
        g = g[~g.id.isin(wynik.keys())]
        roznica = (g.data - e.data).abs().dt.days
        g = g[roznica <= dni]
        if len(g):
            wynik[g.loc[(g.data - e.data).abs().idxmin(), "id"]] = e.klucz
    return pd.Series(wynik, name="klucz", dtype=object)
