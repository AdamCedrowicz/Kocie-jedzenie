"""python -m budzet FOLDER_Z_WYCIAGAMI [--budzet BUDZET.xlsx] [--etykiety POPRAWKI] [--wynik PLIK.xlsx]"""
import argparse
from pathlib import Path

import pandas as pd

from .wczytaj import wczytaj_folder, wczytaj_ustawienia

TU = Path(__file__).parent


def wczytaj_etykiety(sciezka):
    """CSV (id;kategoria) albo xlsx z arkuszem "Do sprawdzenia" (kolumny id, kategoria) -> Series id->kategoria."""
    if not sciezka or not Path(sciezka).exists():
        return pd.Series(dtype=object)
    p = Path(sciezka)
    e = (pd.read_excel(p, sheet_name="Do sprawdzenia", dtype=str) if p.suffix == ".xlsx"
         else pd.read_csv(p, sep=";", dtype=str))
    e = e.dropna(subset=["id", "kategoria"])
    return e.set_index("id").kategoria


def main():
    ap = argparse.ArgumentParser(description="Wczytuje wyciągi bankowe i kategoryzuje transakcje.")
    ap.add_argument("folder")
    ap.add_argument("--ustawienia", default=str(TU / "ustawienia.json"))
    ap.add_argument("--budzet", help="eksport arkusza „Budżet domowy” (.xlsx): kategorie i historia")
    ap.add_argument("--rok", type=int, default=2026)
    ap.add_argument("--reguly", nargs="*", default=[str(TU / "reguly_prywatne.csv"), str(TU / "reguly.csv")])
    ap.add_argument("--etykiety", default=str(TU / "etykiety.csv"),
                    help="Twoje poprawki: CSV id;kategoria albo xlsx z poprawionym arkuszem „Do sprawdzenia”")
    ap.add_argument("--prog", type=float, default=0.6)
    ap.add_argument("--wynik", default="transakcje.xlsx")
    a = ap.parse_args()

    df, raport = wczytaj_folder(a.folder, wczytaj_ustawienia(a.ustawienia))
    for r in raport:
        print(f"{r['plik']}: {r['format']}, wierszy {r['wierszy']}")
        for p in r["problemy"]:
            print(f"   UWAGA: {p}")
    tr = df[df.transfer_wewnetrzny]
    print(f"\nRazem {len(df)} transakcji (po usunięciu duplikatów), "
          f"w tym {len(tr)} transferów wewnętrznych ({(tr.transfer_para != '').sum()} sparowanych).")

    arkusze = {}
    if a.budzet:
        from .kategorie import dopasuj_do_transakcji, etykiety_z_budzetu, wczytaj_kategorie
        from .klasyfikacja import kategoryzuj, ocen, rozwiaz_nazwy, wczytaj_reguly

        kategorie = wczytaj_kategorie(a.budzet)
        reguly, ostrzezenia = rozwiaz_nazwy(wczytaj_reguly(*a.reguly), kategorie)
        for o in ostrzezenia:
            print(f"   UWAGA (reguły): {o}")
        z_budzetu = dopasuj_do_transakcji(df, etykiety_z_budzetu(a.budzet, a.rok))
        df = kategoryzuj(df, reguly, z_budzetu, wczytaj_etykiety(a.etykiety), a.prog)
        jakosc, niezgodne = ocen(df, reguly, z_budzetu, prog=a.prog)
        print("\nKategoryzacja:", df.zrodlo_kategorii.value_counts().to_dict())
        for k, v in jakosc.items():
            print(f"   {k}: {v}")
        print(f"   do sprawdzenia: {df.do_sprawdzenia.sum()}")
        arkusze["Do sprawdzenia"] = df[df.do_sprawdzenia][
            ["id", "data", "kwota", "kontrahent", "tytul", "zrodlo", "kategoria", "pewnosc"]]
        arkusze["Reguła vs budżet"] = niezgodne[["id", "data", "kwota", "kontrahent", "tytul", "regula", "budzet"]]
        arkusze["Kategorie"] = kategorie[["klucz", "grupa", "nazwa"]]

    with pd.ExcelWriter(a.wynik) as xw:
        for nazwa, t in {"Transakcje": df, **arkusze}.items():
            t = t.copy()
            for c in ("data", "data_ksiegowania"):
                if c in t:
                    t[c] = pd.to_datetime(t[c]).dt.date
            t.to_excel(xw, sheet_name=nazwa, index=False)
    print(f"Zapisano: {a.wynik}")


if __name__ == "__main__":
    main()
