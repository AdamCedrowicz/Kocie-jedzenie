"""python -m budzet FOLDER_Z_CSV [--ustawienia PLIK] [--wynik PLIK.xlsx]"""
import argparse
from pathlib import Path

from .wczytaj import wczytaj_folder, wczytaj_ustawienia


def main():
    ap = argparse.ArgumentParser(description="Wczytuje wyciągi bankowe do jednej tabeli.")
    ap.add_argument("folder")
    ap.add_argument("--ustawienia", default=str(Path(__file__).with_name("ustawienia.json")))
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

    wynik = df.copy()
    wynik["data"] = wynik.data.dt.date
    wynik["data_ksiegowania"] = wynik.data_ksiegowania.dt.date
    wynik.to_excel(a.wynik, index=False, sheet_name="Transakcje")
    print(f"Zapisano: {a.wynik}")


if __name__ == "__main__":
    main()
