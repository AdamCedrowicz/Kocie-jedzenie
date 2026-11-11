"""Etap 3: kategoryzacja dwustopniowa.

Kolejność (pierwsza wygrywa): transfer wewnętrzny -> etykieta ręczna -> reguła -> etykieta z budżetu
(historyczna kwota z arkusza) -> model (TF-IDF + regresja logistyczna). Model z pewnością poniżej progu
oznacza transakcję "do sprawdzenia".
"""
import re
from pathlib import Path

import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline

POZA = "POZA BUDŻETEM"
POLA_REGUL = ["kontrahent", "tytul", "opis", "typ"]


# ---------- reguły ----------

def wczytaj_reguly(*sciezki):
    """CSV (;) z kolumnami: kategoria;kontrahent;tytul;opis;typ;kierunek;projekt;uwagi.
    Warunki tekstowe to wyrażenia regularne bez rozróżniania wielkości liter; puste = dowolne.
    kierunek: '+' wpływ, '-' wydatek, puste = oba. Kolejność plików i wierszy = priorytet."""
    czesci = [pd.read_csv(p, sep=";", dtype=str, keep_default_na=False, comment="#")
              for p in sciezki if Path(p).exists()]
    return pd.concat(czesci, ignore_index=True) if czesci else pd.DataFrame(columns=["kategoria"])


def rozwiaz_nazwy(reguly, kategorie):
    """'Nazwa' -> 'Grupa › Nazwa', jeśli nazwa jest w arkuszu jednoznaczna. Zwraca (reguły, ostrzeżenia)."""
    klucze, ostrzezenia = set(kategorie.klucz), []
    po_nazwie = kategorie.groupby("nazwa").klucz.agg(list)
    out = []
    for k in reguly.kategoria:
        if k == POZA or k in klucze:
            out.append(k)
        elif k in po_nazwie and len(po_nazwie[k]) == 1:
            out.append(po_nazwie[k][0])
        else:
            out.append(k)
            ostrzezenia.append(f"kategoria „{k}” " + ("jest niejednoznaczna – podaj „Grupa › Nazwa”"
                                                    if k in po_nazwie else "nie istnieje w arkuszu budżetu"))
    reguly = reguly.copy()
    reguly["kategoria"] = out
    return reguly, sorted(set(ostrzezenia))


def zastosuj_reguly(df, reguly):
    """Zwraca DataFrame(kategoria, projekt) z NaN tam, gdzie żadna reguła nie pasuje."""
    kat = pd.Series(pd.NA, index=df.index, dtype=object)
    proj = pd.Series(pd.NA, index=df.index, dtype=object)
    tekst = {p: df[p].fillna("").astype(str) for p in POLA_REGUL}
    for r in reguly.itertuples():
        maska = kat.isna()
        for p in POLA_REGUL:
            wz = getattr(r, p, "")
            if wz:
                maska &= tekst[p].str.contains(wz, flags=re.IGNORECASE, regex=True)
        if r.kierunek == "+":
            maska &= df.kwota > 0
        elif r.kierunek == "-":
            maska &= df.kwota < 0
        kat[maska] = r.kategoria
        proj[maska] = getattr(r, "projekt", "") or pd.NA
    return pd.DataFrame({"kategoria": kat, "projekt": proj})


# ---------- model ----------

def tekst_do_modelu(df):
    kierunek = df.kwota.map(lambda k: " WPLYW" if k > 0 else " WYDATEK")
    t = (df.kontrahent.fillna("") + " " + df.tytul.fillna("") + " " + df.typ.fillna("") + " "
         + df.kategoria_banku.fillna("") + kierunek)
    return t.str.upper().str.replace(r"\d", " ", regex=True)  # numery sklepów/kas to szum


def trenuj(teksty, etykiety):
    model = make_pipeline(TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True),
                          LogisticRegression(max_iter=3000, C=10))
    return model.fit(teksty, etykiety)


# ---------- całość ----------

def kategoryzuj(df, reguly, etykiety_budzet=None, etykiety_reczne=None, prog=0.6):
    """Dodaje kolumny: kategoria, zrodlo_kategorii, pewnosc, projekt, do_sprawdzenia."""
    df = df.copy()
    df["kategoria"], df["zrodlo_kategorii"], df["pewnosc"] = pd.NA, pd.NA, pd.NA

    def ustaw(maska, kat, zrodlo, pewnosc=1.0):
        m = maska & df.kategoria.isna()
        df.loc[m, "kategoria"] = kat[m] if isinstance(kat, pd.Series) else kat
        df.loc[m, "zrodlo_kategorii"], df.loc[m, "pewnosc"] = zrodlo, pewnosc

    ustaw(df.transfer_wewnetrzny, POZA, "transfer")
    if etykiety_reczne is not None and len(etykiety_reczne):
        r = df.id.map(etykiety_reczne)
        ustaw(r.notna(), r, "ręcznie")
    reg = zastosuj_reguly(df, reguly)
    ustaw(reg.kategoria.notna(), reg.kategoria, "reguła")
    df["projekt"] = reg.projekt
    if etykiety_budzet is not None and len(etykiety_budzet):
        b = df.id.map(etykiety_budzet)
        ustaw(b.notna(), b, "budżet")

    uczace = df[df.kategoria.notna() & (df.kategoria != POZA)]
    reszta = df.kategoria.isna()
    if reszta.any() and uczace.kategoria.nunique() >= 2:
        model = trenuj(tekst_do_modelu(uczace), uczace.kategoria)
        p = model.predict_proba(tekst_do_modelu(df[reszta]))
        df.loc[reszta, "kategoria"] = model.classes_[p.argmax(1)]
        df.loc[reszta, "pewnosc"] = p.max(1).round(2)
        df.loc[reszta, "zrodlo_kategorii"] = "model"
    df["pewnosc"] = df.pewnosc.astype(float)
    df["do_sprawdzenia"] = (df.zrodlo_kategorii == "model") & (df.pewnosc < prog)
    return df


def ocen(df, reguly, etykiety_budzet, od="2026-10-01", prog=0.6):
    """Raport jakości: pokrycie reguł, zgodność reguł z budżetem, model na odłożonym okresie."""
    t = df[~df.transfer_wewnetrzny]
    reg = zastosuj_reguly(t, reguly).kategoria
    bud = t.id.map(etykiety_budzet)
    obie = reg.notna() & bud.notna()
    raport = {
        "transakcji (bez transferów)": len(t),
        "pokrycie reguł": f"{reg.notna().mean():.0%} ({reg.notna().sum()})",
        "reguła vs budżet – zgodne": f"{(reg[obie] == bud[obie]).sum()} z {obie.sum()}",
    }
    # Model: uczy się na znanych etykietach (reguły + budżet) sprzed `od`, test na znanych od `od`.
    # Osobno wynik na "trudnych" (bez reguły) - to te transakcje model obsługuje w praktyce.
    znane = reg.where(reg.notna(), bud)
    znane = znane[znane.notna() & (znane != POZA)]
    przed, po = znane[t.loc[znane.index, "data"] < od], znane[t.loc[znane.index, "data"] >= od]
    if len(po) and przed.nunique() >= 2:
        model = trenuj(tekst_do_modelu(t.loc[przed.index]), przed)
        p = model.predict_proba(tekst_do_modelu(t.loc[po.index]))
        trafione, pewne = model.classes_[p.argmax(1)] == po.values, p.max(1) >= prog
        trudne = reg.loc[po.index].isna().values
        raport[f"model – test od {od}"] = (
            f"trafność {trafione.mean():.0%} na {len(po)}; bez reguły {trafione[trudne].mean():.0%} "
            f"na {trudne.sum()}; pewnych (≥{prog}) {pewne.mean():.0%}, z nich trafnych "
            f"{trafione[pewne].mean():.0%}")
    return raport, t[reg.notna() & bud.notna() & (reg != bud)].assign(regula=reg, budzet=bud)
