import unittest
from decimal import Decimal

from shopper_merge_bot.offer_analysis import (
    analyze_offer,
    classify_category,
    extract_price,
    is_invalid_offer,
    known_filter_categories,
    source_score,
)


class OfferAnalysisTest(unittest.TestCase):
    def test_extract_price_requires_currency_marker(self) -> None:
        self.assertEqual(extract_price("SSD Samsung a 39,99 EUR"), Decimal("39.99"))
        self.assertIsNone(extract_price("Canale con 5047 iscritti"))

    def test_classify_category(self) -> None:
        self.assertEqual(classify_category("Offerta cuffie bluetooth Sony"), "elettronica")
        self.assertEqual(
            classify_category(
                "Condiviso da canale offerte",
                site_text="Elettronica > Cellulari e accessori > Custodie",
            ),
            "elettronica",
        )
        self.assertEqual(
            classify_category(
                "Romanzo in offerta",
                site_text="Libri > Gialli e thriller > Suspense",
            ),
            "libri/thriller",
        )
        self.assertEqual(classify_category("Kinder Bueno 3 pezzi a 2,99 EUR"), "alimentari")
        self.assertIn("libri/thriller", known_filter_categories())
        self.assertNotIn("altro", known_filter_categories())
        self.assertNotEqual(classify_category("Prodotto misterioso in offerta"), "altro")

    def test_classify_user_reported_physical_products(self) -> None:
        self.assertEqual(
            classify_category("COM-FOUR filtri per cappa aspirante ritagliabile su misura"),
            "casa",
        )
        self.assertEqual(
            classify_category("Fischer Viti per Legno o Truciolare A Filetto Parziale, Blu , x mm"),
            "fai-da-te",
        )
        self.assertEqual(
            classify_category("Molotow ONE ALL Inchiostro di Ricarica per Pennarello Indelebile, Blu , ml"),
            "ufficio",
        )
        self.assertEqual(classify_category("SONGMICS - Scarpiera a Livelli"), "casa")
        self.assertEqual(classify_category("HAWKERS One Occhiali da sole Unisex - Adulto"), "moda")
        self.assertEqual(
            classify_category(
                "Cuciture termosaldate Omni-Tech waterproof Cappuccio e polsini regolabili "
                "Poliestere riciclato Ideale per escursioni e citta"
            ),
            "moda",
        )

    def test_classify_user_reported_books(self) -> None:
        self.assertEqual(
            classify_category(
                "Instant Emotions. I segreti delle neuroscienze applicati alle emozioni",
                site_text="libri book books isbn",
            ),
            "libri",
        )
        self.assertEqual(
            classify_category(
                "Nona edizione aggiornata: norme, schemi e casi per districarsi "
                "tra le insidie della procedura penale e restare sempre pronti"
            ),
            "libri",
        )
        self.assertEqual(
            classify_category("Pausa Libro: pillole di scienza e cultura per quando posi il telefono"),
            "libri",
        )

    def test_app_word_does_not_make_physical_products_software(self) -> None:
        self.assertEqual(
            classify_category("App integrata Mini Proiettore Upgraded Proiettore Portatile"),
            "elettronica",
        )
        self.assertEqual(
            classify_category("SanDisk Extreme GB microSDHC Memory Card with App Performance"),
            "elettronica",
        )
        self.assertEqual(
            classify_category(
                "Rete G all avanguardia: condividi l accesso a Internet con dispositivi Wi-Fi "
                "e velocita di download fino a Mbps"
            ),
            "elettronica",
        )
        self.assertEqual(classify_category("Microsoft Office 365 licenza digitale"), "software")

    def test_electronic_accessories_do_not_count_as_electronics(self) -> None:
        self.assertEqual(
            classify_category("Lowepro Tahoe Borsa per Fotocamera"),
            "accessori",
        )
        self.assertEqual(
            classify_category(
                "Trust Bologna Slim Eco Borsa per Laptop fino a, Borsa per Laptop Sostenibile"
            ),
            "accessori",
        )
        self.assertEqual(
            classify_category("Supporto TV parete Super Forte ideale per TV LED/LCD piatti e curvi"),
            "accessori",
        )
        self.assertEqual(
            classify_category("OtterBox Statement Series Studio Cover per iPad Air M"),
            "accessori",
        )
        self.assertEqual(classify_category("Canon Fotocamera Mirrorless"), "elettronica")
        self.assertEqual(classify_category("Samsung TV 55 pollici QLED"), "elettronica")

    def test_user_reported_electronics_and_games_false_positives(self) -> None:
        self.assertEqual(
            classify_category(
                "Lucchetto per TSA Scomparto laptop fino a dimensioni x x cm, peso kg "
                "e L di capacita Cinghie di compressione Ideale per viaggi e ufficio"
            ),
            "viaggi",
        )
        self.assertEqual(classify_category("Smiffys Costume Antico Romano, bianco"), "moda")
        self.assertEqual(classify_category("Cybex Pallas G Plus/Ocean Blue-navy blue PU"), "infanzia")
        self.assertEqual(
            classify_category("Babylino Sensitive Teli Cambio x cm, Traversine letto con assorbenza extra"),
            "infanzia",
        )
        self.assertEqual(
            classify_category("DODOT Pannolini per bambini Activity Taglia, pannolini con vestibilita resistente"),
            "infanzia",
        )
        self.assertEqual(
            classify_category("Cinkee Zanzariera Magnetica per Porta x CM,Rete Fine Zanzariera Magnetica,Tenda"),
            "casa",
        )
        self.assertEqual(
            classify_category("GORMITI - Elesfera del Clan dell Acqua Carter - Giocattolo per Bambini"),
            "giochi",
        )

    def test_product_can_override_broad_site_category(self) -> None:
        self.assertEqual(
            classify_category(
                "Smiffys Costume Antico Romano, bianco",
                site_text="Giochi e giocattoli > Costumi e accessori",
            ),
            "moda",
        )
        self.assertEqual(
            classify_category(
                "Cybex Pallas G Plus/Ocean Blue-navy blue PU",
                site_text="Giochi e giocattoli > Prima infanzia > Seggiolini auto",
            ),
            "infanzia",
        )
        self.assertEqual(
            classify_category(
                "GORMITI - Elesfera del Clan dell Acqua Carter - Giocattolo per Bambini",
                site_text="Elettronica > Accessori",
            ),
            "giochi",
        )

    def test_invalid_offer_detection(self) -> None:
        self.assertTrue(is_invalid_offer("Offerta scaduta, non piu disponibile"))
        self.assertFalse(is_invalid_offer("Offerta lampo ancora attiva"))
        self.assertTrue(
            is_invalid_offer("Estrazione finale a cura di un Notaio o Funzionario Camerale. Regolamento qui")
        )
        self.assertTrue(is_invalid_offer("Offerta finita, link non piu valido"))

    def test_source_score(self) -> None:
        self.assertGreaterEqual(source_score("Junction Bot", "junctionbot", source_type="bot"), 2)
        self.assertGreaterEqual(
            source_score("Offerte Amazon", "deals_channel", source_type="channel"),
            3,
        )
        self.assertGreaterEqual(source_score("Notizie Tech", "newsbot", "notizie", "bot"), 4)

    def test_analyze_offer(self) -> None:
        facts = analyze_offer("Friggitrice ad aria a 49,90 EUR")
        self.assertEqual(facts.category, "casa")
        self.assertEqual(facts.price, Decimal("49.90"))
        self.assertFalse(facts.invalid)
        self.assertFalse(facts.complete)

    def test_complete_offer_requires_product_prices_and_link(self) -> None:
        facts = analyze_offer(
            "Friggitrice ad aria Ninja\nDa 129,99 EUR a 79,99 EUR\nCompra qui",
            ("https://www.amazon.it/Ninja-Friggitrice/dp/B0ABCDEF12?tag=x",),
        )
        self.assertTrue(facts.complete)
        self.assertEqual(facts.product, "Friggitrice ad aria Ninja")
        self.assertEqual(facts.original_price, Decimal("129.99"))
        self.assertEqual(facts.current_price, Decimal("79.99"))
        self.assertEqual(facts.offer_url, "https://amazon.it/dp/B0ABCDEF12")

    def test_offer_url_ignores_media_urls(self) -> None:
        facts = analyze_offer(
            "Cover telefono\nDa 49,99 EUR a 19,99 EUR",
            (
                "https://res.cloudinary.com/demo/image/upload/sample.jpg",
                "https://www.amazon.it/dp/B0ABCDEF12?tag=x",
            ),
        )

        self.assertTrue(facts.complete)
        self.assertEqual(facts.offer_url, "https://amazon.it/dp/B0ABCDEF12")

    def test_analyze_offer_uses_site_category_context(self) -> None:
        facts = analyze_offer(
            "Condiviso da OfferteDale\nDa 179,95 EUR a 109,99 EUR",
            ("https://www.amazon.it/dp/B0FGY8BBVP",),
            site_text="Elettronica > Accessori",
        )

        self.assertEqual(facts.category, "elettronica")

    def test_channel_promo_is_incomplete(self) -> None:
        facts = analyze_offer(
            "OFFERTA TOP SU PRODOTTI FITNESS BELLEZZA E BENESSERE\n"
            "Iscriviti gratis al canale"
        )
        self.assertFalse(facts.complete)
