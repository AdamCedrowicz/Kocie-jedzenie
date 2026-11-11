"""Testy Etapu 3 (reguły, etykiety z budżetu, kolejność źródeł, model).  python -m unittest discover -s budzet/tests -t ."""
import unittest

import pandas as pd

from budzet.kategorie import _kwoty, dopasuj_do_transakcji
from budzet.klasyfikacja import POZA, kategoryzuj, rozwiaz_nazwy, zastosuj_reguly

KATEGORIE = pd.DataFrame([
    {"grupa": "Jedzenie w domu", "nazwa": "Jedzenie dom Adam"},
    {"grupa": "Transport", "nazwa": "Paliwo do auta wspólne"},
    {"grupa": "Mikołowska 49a/2", "nazwa": "Inne Adam"},
    {"grupa": "INNE", "nazwa": "Inne Adam"},
    {"grupa": "Przychody", "nazwa": "Wynajem nieruchomości Adam"},
]).assign(klucz=lambda d: d.grupa + " › " + d.nazwa)


def reguly(*wiersze):
    kol = ["kategoria", "kontrahent", "tytul", "opis", "typ", "kierunek", "projekt"]
    return pd.DataFrame([dict(zip(kol, w)) for w in wiersze], columns=kol).fillna("")


def transakcje(*wiersze):
    df = pd.DataFrame(wiersze, columns=["id", "data", "kwota", "kontrahent", "tytul"])
    df["data"] = pd.to_datetime(df.data)
    return df.assign(opis=df.kontrahent + " " + df.tytul, typ="", kategoria_banku="", transfer_wewnetrzny=False)


class TestKlasyfikacja(unittest.TestCase):
    def test_kwoty_z_komorki_budzetu(self):
        self.assertEqual(_kwoty(12.5), [12.5])
        self.assertEqual(_kwoty("=109.4+77.11"), [109.4, 77.11])
        self.assertEqual(_kwoty("=10+5,20"), [10.0, 5.2])
        self.assertEqual(_kwoty("=SUM(I58:I73)"), [])
        self.assertEqual(_kwoty(None), [])

    def test_nazwy_kategorii(self):
        r, ostrz = rozwiaz_nazwy(reguly(["Jedzenie dom Adam"], ["Inne Adam"], ["Nowa kategoria"], [POZA]), KATEGORIE)
        self.assertEqual(r.kategoria[0], "Jedzenie w domu › Jedzenie dom Adam")
        self.assertEqual(r.kategoria[3], POZA)
        self.assertEqual(len(ostrz), 2)  # "Inne Adam" niejednoznaczna, "Nowa kategoria" nie istnieje

    def test_reguly_regex_kierunek_i_kolejnosc(self):
        df = transakcje(("a", "2026-01-01", -10, "LIDL KOLISTA", ""),
                        ("b", "2026-01-01", 1100, "ANNA TESTOWA", "CZYNSZ"),
                        ("c", "2026-01-01", -50, "ANNA TESTOWA", "zwrot"),
                        ("d", "2026-01-01", -80, "ORLEN STACJA", ""))
        r = reguly(["Wynajem", "ANNA", "CZYNSZ", "", "", "+", "Mikołowska"],
                   ["Jedzenie", "lidl|biedronka"],
                   ["Paliwo", "^ORLEN"], ["Inne", "ORLEN"])
        w = zastosuj_reguly(df, r)
        self.assertEqual(w.kategoria.tolist()[:2], ["Jedzenie", "Wynajem"])
        self.assertTrue(pd.isna(w.kategoria[2]))  # wydatek do tej samej osoby nie jest czynszem
        self.assertEqual(w.kategoria[3], "Paliwo")  # pierwsza pasująca reguła wygrywa
        self.assertEqual(w.projekt[1], "Mikołowska")

    def test_dopasowanie_budzetu_po_kwocie_i_dacie(self):
        df = transakcje(("a", "2026-03-02", -15.5, "SKLEP", ""), ("b", "2026-03-20", -15.5, "SKLEP", ""),
                        ("c", "2026-03-02", 15.5, "ZWROT", ""))
        e = pd.DataFrame({"data": pd.to_datetime(["2026-03-01", "2026-03-10"]), "kwota": [-15.5, -15.5],
                          "klucz": ["K1", "K2"]})
        m = dopasuj_do_transakcji(df, e)
        self.assertEqual(m.to_dict(), {"a": "K1"})  # 10.03 jest za daleko od 20.03; wpływ ma inny znak

    def test_kolejnosc_zrodel_i_model(self):
        sklepy = [(f"s{i}", "2026-01-01", -10, f"BIEDRONKA {i}", "") for i in range(5)]
        stacje = [(f"p{i}", "2026-01-01", -90, f"ORLEN {i}", "") for i in range(5)]
        df = transakcje(*sklepy, *stacje,
                        ("t", "2026-01-02", -500, "JA", ""),          # transfer
                        ("r", "2026-01-02", -7, "BIEDRONKA 9", ""),   # ręcznie nadpisana reguła
                        ("b", "2026-01-02", -33, "COS", ""),          # z budżetu
                        ("m", "2026-01-02", -12, "BIEDRONKA NOWA", ""))  # tylko model
        df.loc[df.id == "t", "transfer_wewnetrzny"] = True
        r = reguly(["Jedzenie", "BIEDRONKA \\d"], ["Paliwo", "ORLEN"])
        w = kategoryzuj(df, r, etykiety_budzet=pd.Series({"b": "Inne"}),
                        etykiety_reczne=pd.Series({"r": "Prezenty"})).set_index("id")
        self.assertEqual(w.loc["t", ["kategoria", "zrodlo_kategorii"]].tolist(), [POZA, "transfer"])
        self.assertEqual(w.loc["r", ["kategoria", "zrodlo_kategorii"]].tolist(), ["Prezenty", "ręcznie"])
        self.assertEqual(w.loc["b", ["kategoria", "zrodlo_kategorii"]].tolist(), ["Inne", "budżet"])
        self.assertEqual(w.loc["m", ["kategoria", "zrodlo_kategorii"]].tolist(), ["Jedzenie", "model"])
        self.assertEqual(w.loc["s0", "zrodlo_kategorii"], "reguła")


if __name__ == "__main__":
    unittest.main()
