"""Etap 2: wczytanie wyciągów (PKO konto, PKO karta kredytowa, mBank, Erste) do jednej tabeli.

Konwencja: kwota < 0 = wydatek, kwota > 0 = wpływ. `data` = data zakupu/operacji.
"""
import csv
import hashlib
import io
import json
import re
from pathlib import Path

import pandas as pd

KOLUMNY = ["id", "zrodlo", "rachunek", "data", "data_ksiegowania", "kwota", "waluta", "kontrahent",
           "rachunek_kontrahenta", "tytul", "typ", "kategoria_banku", "saldo", "opis", "plik",
           "transfer_wewnetrzny", "transfer_para"]


# ---------- drobne pomocnicze ----------

def czytaj_tekst(sciezka):
    raw = Path(sciezka).read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1250")  # PKO eksportuje w windows-1250


def kwota(s):
    """'-1 183,04 PLN' / '+3026.22' / '-4298,86' -> float."""
    s = re.sub(r"[^\d,.\-+]", "", str(s))
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    return float(s) if s.strip("+-") else float("nan")


def nr(s):
    """Numer rachunku bez spacji i apostrofów; polski NRB bez prefiksu PL."""
    s = re.sub(r"[\s'\-]", "", str(s or "")).upper()
    return s[2:] if s.startswith("PL") and len(s) == 28 else s


def spacje(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


def iso(d):
    """'09-10-2026' -> '2026-10-09'; daty ISO bez zmian."""
    m = re.fullmatch(r"(\d{2})-(\d{2})-(\d{4})", d.strip())
    return f"{m[3]}-{m[2]}-{m[1]}" if m else d.strip()


def _pusty(**pola):
    w = {k: "" for k in KOLUMNY}
    w["saldo"] = float("nan")
    w.update(pola)
    return w


# ---------- parsery: tekst pliku -> (lista wierszy, lista problemów) ----------

def pko_konto(tekst):
    wiersze = []
    for w in list(csv.reader(io.StringIO(tekst)))[1:]:
        if not w or not w[0].strip():
            continue
        pola = {}
        for x in w[6:]:
            if ":" in x:
                k, v = x.split(":", 1)
                pola[k.strip()] = spacje(v)
        kontrahent = pola.get("Nazwa odbiorcy") or pola.get("Nazwa nadawcy", "")
        if not kontrahent and "Lokalizacja" in pola:  # płatność kartą debetową
            m = re.search(r"Adres:\s*(.*?)(?:\s+Miasto:|$)", pola["Lokalizacja"])
            kontrahent = m[1] if m else ""
        wiersze.append(_pusty(
            zrodlo="pko_konto", rachunek="PKO konto", data=w[0], data_ksiegowania=w[1], typ=w[2],
            kwota=kwota(w[3]), waluta=w[4], saldo=kwota(w[5]), kontrahent=kontrahent,
            rachunek_kontrahenta=nr(pola.get("Rachunek odbiorcy") or pola.get("Rachunek nadawcy")),
            tytul=pola.get("Tytuł", ""), opis=spacje(" ".join(w[6:]))))
    return wiersze, _kontrola_sald(wiersze)


def pko_karta(tekst):
    wiersze = []
    for w in list(csv.reader(io.StringIO(tekst)))[1:]:
        if not w or not w[0].strip():
            continue
        # W eksporcie "Data operacji" bywa późniejsza niż "Data przetworzenia" - data zakupu to wcześniejsza.
        wiersze.append(_pusty(
            zrodlo="pko_karta", rachunek="PKO karta", data=min(w[0], w[1]), data_ksiegowania=max(w[0], w[1]),
            typ=w[2], kwota=kwota(w[3]), waluta=w[4],
            kontrahent=spacje(re.sub(r"^Tytuł:", "", w[5])), opis=spacje(w[5])))
    return wiersze, []


def mbank(tekst):
    linie = tekst.splitlines()
    start = next(i for i, l in enumerate(linie) if l.lstrip().startswith("#Data operacji"))
    wiersze = []
    for w in csv.reader(linie[start + 1:], delimiter=";"):
        if len(w) < 5 or not re.match(r"\d{4}-\d{2}-\d{2}", w[0].strip()):
            continue
        czesci = [c for c in re.split(r"\s{2,}", w[1].strip()) if c]
        rk = czesci.pop() if czesci and re.fullmatch(r"\d{26}", czesci[-1]) else ""
        typ = czesci.pop() if len(czesci) > 1 else ""
        if len(czesci) > 1 and czesci[0] == typ:  # np. "PRZELEW EXPRESS ELIXIR PRZYCH.  AUTOPAY ..."
            czesci.pop(0)
        kontrahent, _, tytul = czesci[0].partition(", ") if czesci else ("", "", "")
        wiersze.append(_pusty(
            zrodlo="mbank", rachunek=spacje(w[2]), data=w[0].strip(), data_ksiegowania=w[0].strip(),
            typ=typ, kwota=kwota(w[4]), waluta=re.sub(r"[^A-Z]", "", w[4]) or "PLN",
            kontrahent=kontrahent.strip(), tytul=tytul.strip(), rachunek_kontrahenta=rk,
            kategoria_banku=w[3].strip(), opis=spacje(w[1])))

    # Kontrola: suma wpływów i wydatków z nagłówka ("#Waluta;#Wpływy;#Wydatki;").
    problemy = []
    naglowek = next((i for i, l in enumerate(linie) if l.lstrip().startswith("#Waluta;#Wpływy")), None)
    if naglowek is not None:
        for l in linie[naglowek + 1:start]:
            p = l.strip().split(";")
            if len(p) < 3 or not p[0]:
                continue
            kw = [x["kwota"] for x in wiersze if x["waluta"] == p[0]]
            wp, wy = round(sum(k for k in kw if k > 0), 2), round(sum(k for k in kw if k < 0), 2)
            if (wp, wy) != (kwota(p[1]), kwota(p[2])):
                problemy.append(f"mBank {p[0]}: suma wierszy wpływy {wp} / wydatki {wy}, "
                                f"nagłówek {kwota(p[1])} / {kwota(p[2])}")
    return wiersze, problemy


def erste(tekst):
    rows = [r for r in csv.reader(io.StringIO(tekst)) if r]
    meta, rachunek, waluta = rows[0], nr(rows[0][2]), rows[0][4]
    wiersze = []
    for w in rows[1:]:
        if len(w) < 8 or not w[0].strip():
            continue
        wiersze.append(_pusty(
            zrodlo="erste", rachunek=rachunek, data=iso(w[0]), data_ksiegowania=iso(w[1]),
            tytul=spacje(w[2]), kontrahent=spacje(w[3]), rachunek_kontrahenta=nr(w[4]),
            kwota=kwota(w[5]), saldo=kwota(w[6]), waluta=waluta, opis=spacje(w[2] + " " + w[3])))
    problemy = _kontrola_sald(wiersze)
    if meta[7].strip().isdigit() and int(meta[7]) != len(wiersze):
        problemy.append(f"Erste {rachunek}: nagłówek mówi {meta[7]} operacji, wczytano {len(wiersze)}")
    return wiersze, problemy


def _kontrola_sald(wiersze):
    """Wiersze są od najnowszego: saldo[i] = saldo[i+1] + kwota[i]."""
    problemy = []
    for a, b in zip(wiersze, wiersze[1:]):
        if abs(b["saldo"] + a["kwota"] - a["saldo"]) > 0.005:
            problemy.append(f"{a['zrodlo']} {a['data']} {a['kwota']:+.2f}: saldo {a['saldo']:.2f} "
                            f"nie wynika z poprzedniego {b['saldo']:.2f}")
    return problemy


PARSERY = {"pko_konto": pko_konto, "pko_karta": pko_karta, "mbank": mbank, "erste": erste}


def rozpoznaj(tekst):
    if "#Data operacji" in tekst:
        return "mbank"
    pierwsza = tekst.splitlines()[0] if tekst else ""
    if pierwsza.startswith('"Data operacji","Data waluty","Typ transakcji"'):
        return "pko_konto"
    if pierwsza.startswith('"Data operacji","Data przetworzenia"'):
        return "pko_karta"
    w = next(csv.reader([pierwsza]), [])
    if len(w) >= 8 and w[2].startswith("'"):
        return "erste"
    raise ValueError("nieznany format wyciągu")


# ---------- całość ----------

def wczytaj_plik(sciezka):
    tekst = czytaj_tekst(sciezka)
    fmt = rozpoznaj(tekst)
    wiersze, problemy = PARSERY[fmt](tekst)
    df = pd.DataFrame(wiersze, columns=KOLUMNY)
    df["plik"] = Path(sciezka).name
    # id: te same operacje z nakładających się eksportów dostają ten sam id;
    # n-ty identyczny wiersz w pliku (np. 2x ta sama kawa) dostaje inny id.
    klucz = (df.zrodlo + "|" + df.rachunek + "|" + df.data + "|" + df.kwota.map("{:.2f}".format) + "|"
             + df.opis.str.upper().str.replace(r"\W", "", regex=True))
    df["id"] = [hashlib.sha1(f"{k}|{n}".encode()).hexdigest()[:12]
                for k, n in zip(klucz, klucz.groupby(klucz).cumcount())]
    return df, fmt, problemy


def oznacz_transfery(df, moje_rachunki, kontrahenci_wewnetrzni=(), rachunki_przejsciowe=(), dni=3):
    """Transfer wewnętrzny = przelew na/z własnego rachunku (także numer w tytule, np. AUTOPAY),
    kontrahent z listy, każda operacja na rachunku przejściowym (np. Erste EUR: przychód liczy się
    dopiero jako PLN z kantoru), albo druga strona takiego przelewu w innym źródle
    (ta sama kwota z przeciwnym znakiem, do `dni` dni różnicy)."""
    moje = {nr(x) for x in moje_rachunki}
    zwarty = (df.opis + df.tytul).str.replace(r"\s", "", regex=True).str.upper()
    flaga = df.rachunek_kontrahenta.isin(moje) | zwarty.map(lambda t: any(m in t for m in moje))
    for wz in kontrahenci_wewnetrzni:
        flaga |= df.kontrahent.str.contains(wz, case=False, regex=False)  # tylko kontrahent, nie tytuł
    flaga |= df.rachunek.isin({nr(x) for x in rachunki_przejsciowe})

    para = pd.Series("", index=df.index)
    konto = df.zrodlo + df.rachunek
    for i in df.index[flaga]:
        if para[i]:
            continue
        kand = df[(para == "") & (konto != konto[i]) & (df.waluta == df.waluta[i])
                  & ((df.kwota + df.kwota[i]).abs() < 0.005)
                  & ((df.data - df.data[i]).abs().dt.days <= dni)]
        if len(kand):
            j = (kand.data - df.data[i]).abs().idxmin()
            para[i], para[j] = df.id[j], df.id[i]
            flaga[j] = True
    df["transfer_wewnetrzny"], df["transfer_para"] = flaga, para
    return df


def wczytaj_folder(folder, ustawienia=None):
    """Zwraca (tabela transakcji, raport) dla wszystkich *.csv w folderze."""
    ust = ustawienia or {}
    czesci, raport = [], []
    for p in sorted(Path(folder).glob("*.csv")):
        try:
            df, fmt, problemy = wczytaj_plik(p)
        except Exception as e:  # jeden zły plik nie blokuje reszty
            raport.append({"plik": p.name, "format": "?", "wierszy": 0, "problemy": [str(e)]})
            continue
        czesci.append(df)
        raport.append({"plik": p.name, "format": fmt, "wierszy": len(df), "problemy": problemy})
    df = pd.concat(czesci, ignore_index=True) if czesci else pd.DataFrame(columns=KOLUMNY)
    df = df.drop_duplicates("id").reset_index(drop=True)
    df["data"] = pd.to_datetime(df.data)
    df["data_ksiegowania"] = pd.to_datetime(df.data_ksiegowania)
    df = oznacz_transfery(df, ust.get("moje_rachunki", []), ust.get("kontrahenci_wewnetrzni", []),
                          ust.get("rachunki_przejsciowe", []))
    return df.sort_values(["data", "zrodlo"], ascending=[False, True]).reset_index(drop=True), raport


def wczytaj_ustawienia(sciezka):
    p = Path(sciezka)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
