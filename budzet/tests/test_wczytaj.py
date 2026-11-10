"""Testy Etapu 2 na zmyślonych próbkach w formatach banków.  Uruchom: python -m unittest discover budzet/tests"""
import shutil
import tempfile
import unittest
from pathlib import Path

from budzet.wczytaj import kwota, wczytaj_folder, wczytaj_plik

MOJE = {"moje_rachunki": ["11 1111 1111 1111 1111 1111 1111",   # PKO konto
                          "22 2222 2222 2222 2222 2222 2222",   # mBank
                          "33 3333 3333 3333 3333 3333 3333"],  # Erste
        "kontrahenci_wewnetrzni": ["KANTOR TEST"],
        "rachunki_przejsciowe": ["77 7777 7777 7777 7777 7777 7777"]}  # Erste EUR

PKO_KONTO = (
    '"Data operacji","Data waluty","Typ transakcji","Kwota","Waluta","Saldo po transakcji","Opis transakcji","","","",""\n'
    '"2026-10-12","2026-10-12","Przelew na konto","+3000.00","PLN","+3100.00","Rachunek nadawcy: 33 3333 3333 3333 3333 3333 3333","Nazwa nadawcy: MOJA FIRMA","Tytuł: Wpłata do PKO","",""\n'
    '"2026-10-05","2026-10-05","Przelew z rachunku","-400.00","PLN","+100.00","Rachunek odbiorcy: 44 4444 4444 4444 4444 4444 4444","Nazwa odbiorcy: JAN TESTOWY","Tytuł: GARAŻ","",""\n'
    '"2026-10-01","2026-10-01","Płatność kartą","-0.50","PLN","+500.00","Tytuł: 000123 KAWA","Lokalizacja: Adres: KAWIARNIA X Miasto: KATOWICE Kraj: POLSKA","","",""\n'
)

PKO_KARTA = (
    '"Data operacji","Data przetworzenia","Rodzaj operacji","Kwota operacji","Waluta","Opis"\n'
    '"2026-11-10","2026-11-09","Płatność kartą","-37.20","PLN","Tytuł: SKLEP TESTOWY         Katowice     POL"\n'
    '"2026-11-10","2026-11-09","Płatność kartą","-5.00","PLN","Tytuł: KAWA                   Katowice     POL"\n'
    '"2026-11-10","2026-11-09","Płatność kartą","-5.00","PLN","Tytuł: KAWA                   Katowice     POL"\n'
)

MBANK = (
    "﻿mBank S.A. Bankowość Detaliczna;\r\n\r\n#Klient;\r\nJAN TESTOWY;\r\n\r\n"
    "      #Waluta;#Wpływy;#Wydatki;\r\nPLN;9 619,00;-1 183,04;\r\n      \r\n"
    "#Data operacji;#Opis operacji;#Rachunek;#Kategoria;#Kwota;\r\n"
    '2026-10-23;"KREDYT - SPŁATA RATY                ";"mKonto 2222 ... 2222";"Spłaty rat";-1 183,04 PLN;;\r\n'
    '2026-10-21;"PRZELEW EXPRESS ELIXIR PRZYCH.  AUTOPAY SPÓŁKA AKCYJNA        PRZELEW EXPRESS ELIXIR PRZYCH.          55555555555555555555555555  ";"mKonto 2222 ... 2222";"Wpływy - inne";9 619,00 PLN;;\r\n'
)

ERSTE = (
    "2026-11-11,06-10-2026,'33 3333 3333 3333 3333 3333 3333,MOJA FIRMA,PLN,\"0,00\",\"0,00\",3,\n"
    "21-10-2026,21-10-2026,Przelew,Ja mBank,22 2222 2222 2222 2222 2222 2222,\"-9619,00\",\"0,00\",1,\n"
    "12-10-2026,10-10-2026,Wpłata do PKO,Ja PKO,11 1111 1111 1111 1111 1111 1111,\"-3000,00\",\"9619,00\",2,\n"
    "09-10-2026,09-10-2026,FV-1,KLIENT SP. Z O.O.,66 6666 6666 6666 6666 6666 6666,\"12619,00\",\"12619,00\",3,\n"
)


def zapisz(folder, nazwa, tekst, kodowanie="utf-8"):
    p = Path(folder) / nazwa
    p.write_bytes(tekst.encode(kodowanie))
    return p


class TestWczytaj(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        zapisz(self.dir, "pko.csv", PKO_KONTO, "cp1250")
        zapisz(self.dir, "karta.csv", PKO_KARTA, "cp1250")
        zapisz(self.dir, "mbank.csv", MBANK)
        zapisz(self.dir, "erste.csv", ERSTE)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_kwoty(self):
        self.assertEqual(kwota("-1 183,04 PLN"), -1183.04)
        self.assertEqual(kwota("+3026.22"), 3026.22)
        self.assertEqual(kwota("-4298,86"), -4298.86)
        self.assertEqual(kwota("1 100,00 PLN"), 1100.0)

    def test_pko_konto(self):
        df, fmt, problemy = wczytaj_plik(Path(self.dir) / "pko.csv")
        self.assertEqual((fmt, len(df), problemy), ("pko_konto", 3, []))
        w = df.iloc[1]
        self.assertEqual((w.kontrahent, w.tytul, w.rachunek_kontrahenta),
                         ("JAN TESTOWY", "GARAŻ", "44444444444444444444444444"))
        self.assertEqual(df.iloc[2].kontrahent, "KAWIARNIA X")

    def test_pko_konto_zle_saldo(self):
        zapisz(self.dir, "pko.csv", PKO_KONTO.replace('"+100.00"', '"+150.00"'), "cp1250")
        _, _, problemy = wczytaj_plik(Path(self.dir) / "pko.csv")
        self.assertTrue(problemy)

    def test_karta_data_zakupu_to_wczesniejsza(self):
        df, fmt, _ = wczytaj_plik(Path(self.dir) / "karta.csv")
        self.assertEqual(fmt, "pko_karta")
        self.assertEqual((df.data[0], df.data_ksiegowania[0]), ("2026-11-09", "2026-11-10"))
        self.assertEqual(df.kontrahent[0], "SKLEP TESTOWY Katowice POL")

    def test_mbank(self):
        df, fmt, problemy = wczytaj_plik(Path(self.dir) / "mbank.csv")
        self.assertEqual((fmt, len(df), problemy), ("mbank", 2, []))
        w = df.iloc[1]
        self.assertEqual((w.kwota, w.kontrahent, w.typ, w.rachunek_kontrahenta, w.kategoria_banku),
                         (9619.0, "AUTOPAY SPÓŁKA AKCYJNA", "PRZELEW EXPRESS ELIXIR PRZYCH.",
                          "55555555555555555555555555", "Wpływy - inne"))

    def test_mbank_niekompletny_eksport(self):
        zapisz(self.dir, "mbank.csv", MBANK.replace("PLN;9 619,00;", "PLN;9 700,00;"))
        _, _, problemy = wczytaj_plik(Path(self.dir) / "mbank.csv")
        self.assertEqual(len(problemy), 1)

    def test_erste(self):
        df, fmt, problemy = wczytaj_plik(Path(self.dir) / "erste.csv")
        self.assertEqual((fmt, len(df), problemy), ("erste", 3, []))
        self.assertEqual((df.data[1], df.kwota[1], df.rachunek[1]),
                         ("2026-10-12", -3000.0, "33333333333333333333333333"))

    def test_ten_sam_plik_dwa_razy_nie_dubluje(self):
        df1, _ = wczytaj_folder(self.dir, MOJE)
        shutil.copy(Path(self.dir) / "karta.csv", Path(self.dir) / "karta_kopia.csv")
        shutil.copy(Path(self.dir) / "mbank.csv", Path(self.dir) / "mbank_kopia.csv")
        df2, _ = wczytaj_folder(self.dir, MOJE)
        self.assertEqual(len(df1), len(df2))
        self.assertEqual(len(df1), 3 + 3 + 2 + 3)  # dwie identyczne kawy zostają jako dwie

    def test_transfery_wewnetrzne_z_obu_stron(self):
        df, _ = wczytaj_folder(self.dir, MOJE)
        tr = df[df.transfer_wewnetrzny]
        self.assertEqual(sorted(tr.kwota), [-9619.0, -3000.0, 3000.0, 9619.0])
        # mBank-owy AUTOPAY nie ma mojego numeru w opisie - złapany tylko przez parowanie
        autopay = df[(df.zrodlo == "mbank") & (df.kwota == 9619)].iloc[0]
        erste = df[(df.zrodlo == "erste") & (df.kwota == -9619)].iloc[0]
        self.assertEqual((autopay.transfer_para, erste.transfer_para), (erste.id, autopay.id))
        # zwykłe wydatki i przychody nie są transferami
        self.assertFalse(df[df.kontrahent == "JAN TESTOWY"].transfer_wewnetrzny.any())
        self.assertFalse(df[df.kwota == 12619].transfer_wewnetrzny.any())

    def test_kontrahent_wewnetrzny_z_listy(self):
        zapisz(self.dir, "erste.csv", ERSTE.replace("KLIENT SP. Z O.O.", "KANTOR TEST SP. Z O.O."))
        df, _ = wczytaj_folder(self.dir, MOJE)
        self.assertTrue(df[df.kwota == 12619].transfer_wewnetrzny.all())

    def test_rachunek_przejsciowy_eur_nie_liczy_sie_a_pln_z_kantoru_tak(self):
        zapisz(self.dir, "erste_eur.csv",
               "2026-11-11,06-10-2026,'77 7777 7777 7777 7777 7777 7777,MOJA FIRMA,EUR,\"0,00\",\"0,00\",2,\n"
               "09-10-2026,09-10-2026,JJ2F20,SUPER GRUPA PL,88 8888 8888 8888 8888 8888 8888,\"-4300,00\",\"0,00\",1,\n"
               "09-10-2026,09-10-2026,invoice FV-1,KLIENT ZAGRANICZNY,CH00 0000 0000,\"4300,00\",\"4300,00\",2,\n")
        zapisz(self.dir, "erste.csv", ERSTE.replace("KLIENT SP. Z O.O.", "SUPER GRUPA PL"))
        df, _ = wczytaj_folder(self.dir, MOJE)
        self.assertTrue(df[df.waluta == "EUR"].transfer_wewnetrzny.all())
        self.assertFalse(df[(df.kwota == 12619) & (df.waluta == "PLN")].transfer_wewnetrzny.any())

    def test_nieznany_plik_nie_blokuje_reszty(self):
        zapisz(self.dir, "smieci.csv", "a,b,c\n1,2,3\n")
        df, raport = wczytaj_folder(self.dir, MOJE)
        self.assertEqual(len(df), 11)
        self.assertTrue(any(r["format"] == "?" for r in raport))


if __name__ == "__main__":
    unittest.main()
