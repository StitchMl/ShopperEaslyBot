from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache

from .normalization import canonicalize_url, extract_urls, normalize_text


PRICE_RE = re.compile(
    r"(?:(?:eur|euro|\u20ac)\s*)?(\d{1,5}(?:[.,]\d{1,2})?)\s*(?:\u20ac|eur|euro)?",
    re.IGNORECASE,
)

INVALID_PATTERNS = (
    r"\bscadut[aoei]?\b",
    r"\bterminat[aoei]?\b",
    r"\besaurit[aoei]?\b",
    r"\bsold\s*out\b",
    r"\bexpired\b",
    r"non\s+(?:piu\s+)?disponibile",
    r"offerta\s+(?:non\s+)?(?:piu\s+)?valida",
    r"offerta\s+(?:chiusa|finita|esaurita)",
    r"deal\s+(?:chiuso|finito|expired)",
    r"coupon\s+(?:non\s+)?(?:piu\s+)?valido",
    r"codice\s+(?:scaduto|non\s+valido)",
    r"link\s+(?:non\s+)?(?:piu\s+)?valido",
    r"prezzo\s+(?:salito|aumentato|cambiato)",
    r"\bestrazione\s+finale\b",
    r"\bfunzionario\s+camerale\b",
    r"\bregolamento\s+qui\b",
    r"\bnotaio\b",
)

MEDIA_URL_EXTENSIONS = (
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".avif",
    ".mp4",
    ".webm",
)
MEDIA_URL_HOSTS = (
    "res.cloudinary.com",
    "images-na.ssl-images-amazon.com",
    "m.media-amazon.com",
)

GENERIC_PRODUCT_PATTERNS = (
    r"^sconto\s+del\s+fino\s+a\s+esaurimento\s+scorte\b",
    r"^sconto\s+del\b",
    r"^a\s+soli\s+invece\s+di(?:\s+di\s+sconto)?$",
    r"^invece\s+di(?:\s+di\s+sconto)?$",
    r"^condividi\s+\S+$",
    r"^segnalat[ao]\s+su\b",
    r"^segnalat[ao]\s+sull\b",
    r"^occasione\s+su\b",
    r"^minimo\s+storico$",
    r"^prices\s+updated\s+on\b",
    r"^how\s+to\s+come\s+usare\s+i\s+coupon\b",
    r"^i\s+prezzi\s+possono\s+subire\s+variazioni\b",
    r"^disclaimer$",
    r"^ad\s+info$",
)

CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "accessori": (
        "accessori",
        "accessorio",
        "borsa per fotocamera",
        "borsa fotocamera",
        "borsa per camera",
        "borsa camera",
        "borsa per laptop",
        "borsa laptop",
        "borsa per notebook",
        "borsa notebook",
        "borsa per tablet",
        "borsa tablet",
        "borsa pc",
        "zaino per laptop",
        "zaino laptop",
        "zaino per notebook",
        "zaino notebook",
        "custodia",
        "custodie",
        "cover",
        "case",
        "protezione schermo",
        "pellicola",
        "vetro temperato",
        "supporto tv",
        "supporto televisore",
        "supporto monitor",
        "supporto parete",
        "supporto da parete",
        "staffa tv",
        "staffa per tv",
        "wall mount",
        "otterbox",
        "lowepro",
    ),
    "elettronica": (
        "smartphone",
        "telefono",
        "iphone",
        "samsung",
        "xiaomi",
        "tablet",
        "pc",
        "notebook",
        "laptop",
        "monitor",
        "ssd",
        "hard disk",
        "microsd",
        "micro sd",
        "scheda sd",
        "scheda memoria",
        "memory card",
        "router",
        "wi-fi",
        "wifi",
        "internet",
        "mbps",
        "rete 5g",
        "5g",
        "cuffie",
        "auricolari",
        "speaker",
        "bluetooth",
        "tv",
        "televisore",
        "magsafe",
        "caricatore",
        "powerbank",
        "cavo usb",
        "usb-c",
        "fotocamera",
        "telecamera",
        "videocamera",
        "proiettore",
        "projector",
        "console",
        "playstation",
        "xbox",
        "nintendo",
        "apple",
        "anker",
        "ugreen",
        "sandisk",
        "logitech",
        "tp-link",
        "tplink",
        "huawei",
        "honor",
        "oppo",
        "realme",
        "oneplus",
        "lenovo",
        "asus",
        "acer",
        "hewlett-packard",
        "hp pavilion",
        "hp elitebook",
        "hp envy",
        "hp omen",
        "hp deskjet",
        "hp laserjet",
        "bose",
        "sony",
        "jbl",
        "soundbar",
        "mouse",
        "tastiera",
        "stampante",
        "smartwatch",
        "drone",
    ),
    "casa": (
        "casa",
        "cucina",
        "friggitrice",
        "air fryer",
        "aspirapolvere",
        "cappa",
        "cappe",
        "filtro cappa",
        "filtri cappa",
        "robot",
        "lavatrice",
        "lavastoviglie",
        "forno",
        "microonde",
        "pentola",
        "pentole",
        "padella",
        "padelle",
        "pirofile",
        "alluminio",
        "tazza",
        "bottiglia",
        "organizer",
        "materasso",
        "lenzuola",
        "divano",
        "sedia",
        "lampada",
        "lampadine",
        "lampadina",
        "illuminazione",
        "barbecue",
        "barbecue a gas",
        "grill",
        "weber",
        "ripiani laterali",
        "terrazze",
        "balconi",
        "bruciatore",
        "giardino",
        "bosch",
        "contenitore",
        "scatola",
        "copriletto",
        "cuscino",
        "coperta",
        "tappeto",
        "zanzariera",
        "zanzariera magnetica",
        "rete fine",
        "tenda",
        "appendiabiti",
        "mensola",
        "scaffale",
        "scarpiera",
        "portascarpe",
        "portaoggetti",
        "guardaroba",
        "cassettiera",
        "cassetti",
        "mobile",
        "armadio",
        "pulizia",
        "detersivo",
        "ammorbidente",
        "candela",
    ),
    "moda": (
        "scarpe",
        "sneaker",
        "maglia",
        "felpa",
        "giacca",
        "jeans",
        "borsa",
        "zaino",
        "zainetto",
        "orologio",
        "abbigliamento",
        "moda",
        "vestito",
        "intimo",
        "pantaloni",
        "camicia",
        "cappotto",
        "giubbotto",
        "giacchetto",
        "jacket",
        "impermeabile",
        "waterproof",
        "cappuccio",
        "polsini",
        "poliestere",
        "calze",
        "calzini",
        "t-shirt",
        "t shirt",
        "polo",
        "shorts",
        "slip",
        "reggiseno",
        "cintura",
        "portafoglio",
        "costume",
        "travestimento",
        "occhiali",
        "occhiali da sole",
        "unisex",
        "uomo",
        "donna",
    ),
    "bellezza": (
        "beauty",
        "bellezza",
        "crema",
        "profumo",
        "rasoio",
        "spazzolino",
        "shampoo",
        "cosmetico",
        "makeup",
        "make up",
        "fondotinta",
        "incarnato",
        "acido ialuronico",
        "trucco",
        "correttore",
        "mascara",
        "rossetto",
        "labbra",
        "smalto",
        "smalti",
        "unghie",
        "manicure",
        "nail",
        "trimmer",
        "phon",
        "epilatore",
        "piastra capelli",
        "deodorante",
        "dentifricio",
        "integratore",
        "collagene",
        "protezione solare",
        "solare",
        "maschera viso",
        "siero",
        "balsamo",
        "spazzola",
        "oral-b",
        "gillette",
        "braun",
        "remington",
    ),
    "sport": (
        "sport",
        "fitness",
        "palestra",
        "bicicletta",
        "bike",
        "tapis roulant",
        "running",
        "trekking",
        "calcio",
        "padel",
        "yoga",
        "manubri",
        "pesi",
        "tuta",
        "scarpe running",
        "racchetta",
        "campeggio",
        "zaino trekking",
    ),
    "giochi": (
        "lego",
        "gioco",
        "giochi",
        "giocattolo",
        "videogioco",
        "videogiochi",
        "nintendo",
        "super mario",
        "mario bros",
        "switch",
        "boardgame",
        "puzzle",
        "nerf",
        "pokemon",
        "barbie",
        "action figure",
        "peluche",
        "carte collezionabili",
        "star wars",
        "hello kitty",
        "disney",
        "marvel",
        "playmobil",
        "hot wheels",
        "bambola",
        "bambole",
        "costruzioni",
    ),
    "infanzia": (
        "prima infanzia",
        "neonato",
        "neonati",
        "bambini",
        "bambino",
        "bambina",
        "pannolini",
        "pannolino",
        "teli cambio",
        "telo cambio",
        "traversine",
        "traversina",
        "seggiolino",
        "seggiolini",
        "seggiolone",
        "passeggino",
        "lettino",
        "fasciatoio",
        "ciuccio",
        "biberon",
        "cybex",
        "pallas",
        "dodot",
        "babylino",
    ),
    "libri": (
        "libro",
        "libri",
        "kindle",
        "ebook",
        "audible",
        "manuale",
        "manuali",
        "edizione",
        "autore",
        "pagine",
        "saggio",
        "scienza",
        "cultura",
        "fumetto",
        "manga",
    ),
    "auto": (
        "auto",
        "moto",
        "casco",
        "automotive",
        "tergicristallo",
        "batteria auto",
        "portapacchi",
        "supporto smartphone auto",
        "dashcam",
        "pneumatici",
        "olio motore",
        "avviatore",
        "compressore",
        "carplay",
        "autoradio",
        "lavavetri",
        "coprisedile",
        "catene neve",
    ),
    "viaggi": (
        "viaggi",
        "viaggio",
        "valigia",
        "valigie",
        "trolley",
        "bagaglio",
        "bagagli",
        "samsonite",
        "zaino con ruote",
        "con ruote",
        "lucchetto tsa",
        "lucchetto",
        "tsa",
        "scomparto laptop",
        "cinghie di compressione",
        "capacita",
        "capacità",
    ),
    "fai-da-te": (
        "fai da te",
        "bricolage",
        "ferramenta",
        "viti",
        "vite",
        "tasselli",
        "tassello",
        "bulloni",
        "bullone",
        "legno",
        "truciolare",
        "filetto",
        "fischer",
        "utensile",
        "utensili",
        "trapano",
        "avvitatore",
        "ricambio",
        "ricambi",
        "paranco",
        "puleggia",
        "argano",
        "sollevatore",
        "sollevamento",
        "motore monofase",
        "ribimex",
    ),
    "ufficio": (
        "cancelleria",
        "prodotti per ufficio",
        "ufficio",
        "inchiostro",
        "pennarello",
        "pennarelli",
        "marcatore",
        "marcatori",
        "evidenziatore",
        "evidenziatori",
        "matita",
        "matite",
        "penna",
        "penne",
        "scrittura",
        "cartucce",
        "cartuccia",
        "toner",
        "inkjet",
        "deskjet",
        "officejet",
        "stampante",
    ),
    "alimentari": (
        "caffe",
        "caffè",
        "pasta",
        "vino",
        "olio",
        "cioccolato",
        "cioccolata",
        "kinder",
        "nutella",
        "biscotti",
        "tonno",
        "riso",
        "salsa",
        "sugo",
        "the",
        "tisana",
        "snack",
        "alimentari",
        "food",
        "bevanda",
        "birra",
        "whisky",
        "gin",
        "acqua",
        "succhi",
        "patatine",
        "caramelle",
        "barrette",
        "proteine",
        "cibo",
    ),
    "animali": (
        "animali",
        "animali domestici",
        "pet",
        "gatto",
        "gatti",
        "cane",
        "cani",
        "leccornia",
        "croccantini",
        "lettiera",
        "snack per gatti",
        "snack per cani",
        "filetto di tonno",
        "inaba",
    ),
    "musica": (
        "strumenti musicali",
        "strumento musicale",
        "bacchette",
        "bacchette per batteria",
        "drumsticks",
        "vic firth",
        "hickory",
        "punta di legno",
        "chitarra",
        "pianoforte",
        "spartito",
        "accordatore",
    ),
    "software": (
        "software",
        "vpn",
        "licenza",
        "windows",
        "microsoft office",
        "office 365",
        "antivirus",
        "abbonamento",
        "app mobile",
        "applicazione mobile",
        "applicazioni mobile",
        "steam",
        "playstation plus",
        "xbox game pass",
        "gift card",
        "codice digitale",
        "cloud storage",
    ),
}

BOOK_GENRE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "libri/fantasy": (
        "fantasy",
        "urban fantasy",
        "fantascienza",
        "sci fi",
        "science fiction",
        "distopia",
    ),
    "libri/thriller": (
        "thriller",
        "giallo",
        "gialli",
        "crime",
        "noir",
        "mistero",
        "suspense",
        "poliziesco",
    ),
    "libri/romanzi": (
        "romanzo",
        "romanzi",
        "narrativa",
        "letteratura",
        "rosa",
        "romance",
        "contemporanea",
    ),
    "libri/bambini": (
        "bambini",
        "ragazzi",
        "young adult",
        "infanzia",
        "adolescenti",
    ),
    "libri/fumetti": (
        "fumetto",
        "fumetti",
        "manga",
        "graphic novel",
        "comics",
    ),
    "libri/manuali": (
        "manuale",
        "manuali",
        "business",
        "economia",
        "informatica",
        "programmazione",
        "cucina",
        "self-help",
        "crescita personale",
    ),
}

SITE_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "elettronica": (
        "elettronica",
        "informatica",
        "telefonia",
        "cellulari",
        "computer",
        "audio",
        "video",
        "tv e home cinema",
        "accessori per cellulari",
    ),
    "accessori": (
        "custodie",
        "cover",
        "borse per fotocamera",
        "borse per laptop",
        "borse per notebook",
        "supporti tv",
        "supporti da parete",
        "staffa tv",
        "accessori per tablet",
    ),
    "casa": (
        "casa e cucina",
        "casa",
        "cucina",
        "giardino",
        "illuminazione",
        "arredamento",
        "elettrodomestici",
        "barbecue",
        "grill",
    ),
    "moda": (
        "abbigliamento",
        "scarpe",
        "borse",
        "gioielli",
        "orologi",
        "moda",
    ),
    "bellezza": (
        "bellezza",
        "salute",
        "cura della persona",
        "profumi",
        "cosmetici",
        "make up",
        "makeup",
        "fondotinta",
        "igiene",
    ),
    "sport": (
        "sport",
        "tempo libero",
        "outdoor",
        "fitness",
        "palestra",
    ),
    "giochi": (
        "giochi e giocattoli",
        "giocattoli",
        "videogiochi",
        "nintendo",
        "hobby",
    ),
    "infanzia": (
        "prima infanzia",
        "neonati",
        "pannolini",
        "seggiolini auto",
        "seggioloni",
        "passeggini",
        "fasciatoi",
    ),
    "libri": (
        "libri",
        "book",
        "books",
        "kindle store",
        "audible",
        "letteratura",
        "narrativa",
    ),
    "auto": (
        "auto e moto",
        "automotive",
        "accessori auto",
        "ricambi",
        "moto",
    ),
    "viaggi": (
        "valigeria",
        "bagagli",
        "accessori da viaggio",
        "zaini e borse da viaggio",
        "trolley",
        "lucchetti per bagagli",
    ),
    "fai-da-te": (
        "fai da te",
        "bricolage",
        "ferramenta",
        "utensili",
        "utensili elettrici",
        "sollevamento",
        "paranchi",
        "paranco",
        "viti",
        "bulloni",
        "tasselli",
    ),
    "ufficio": (
        "cancelleria",
        "prodotti per ufficio",
        "penne",
        "matite",
        "scrittura",
        "marcatori",
        "evidenziatori",
        "inchiostro",
        "toner",
        "cartucce inchiostro",
        "cartucce",
        "accessori per stampanti",
        "stampanti e accessori",
        "inkjet",
    ),
    "alimentari": (
        "alimentari",
        "supermercato",
        "food",
        "bevande",
        "drogheria",
    ),
    "animali": (
        "animali",
        "animali domestici",
        "prodotti per animali",
        "cani",
        "gatti",
        "pet shop",
    ),
    "musica": (
        "strumenti musicali",
        "musica",
        "batterie e percussioni",
        "percussioni",
        "chitarre",
    ),
    "software": (
        "software",
        "applicazioni",
        "licenze",
        "antivirus",
        "download digitale",
    ),
}

HIGH_CONFIDENCE_CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "software",
        (
            r"\b(?:licenza digitale|codice digitale|codice download|download digitale|steam key)\b",
            r"\b(?:office 365|microsoft office|antivirus|vpn|xbox game pass|playstation plus)\b",
            r"\bcodice ea app\b",
        ),
    ),
    (
        "elettronica",
        (
            r"\b(?:air tag|airtag|tracker wallet|localizzatore bluetooth)\b",
            r"\b(?:instax|pellicola istantanea)\b",
        ),
    ),
    (
        "viaggi",
        (
            r"\b(?:valigia|valigie|trolley|bagaglio|bagagli|lucchetto tsa)\b",
            r"\b(?:samsonite|zaino con ruote|zaino .{0,40} con ruote|cinghie di compressione)\b",
        ),
    ),
    (
        "accessori",
        (
            r"\b(?:cover|custodia|custodie|pellicola|vetro temperato)\b",
            r"\b(?:borsa|zaino)\s+(?:per\s+)?(?:fotocamera|laptop|notebook|tablet|pc)\b",
            r"\b(?:supporto|staffa)\s+(?:tv|televisore|monitor|parete)\b",
            r"\b(?:otterbox|lowepro)\b",
        ),
    ),
    (
        "infanzia",
        (
            r"\b(?:pannolini|pannolino|traversine|teli cambio|telo cambio)\b",
            r"\b(?:seggiolino|seggiolini|passeggino|fasciatoio|lettino|cybex|pallas|dodot|babylino)\b",
        ),
    ),
    (
        "animali",
        (
            r"\b(?:gatto|gatti|cane|cani|croccantini|lettiera|leccornia|inaba)\b",
            r"\b(?:snack per gatti|snack per cani|filetto di tonno)\b",
        ),
    ),
    (
        "bellezza",
        (
            r"\b(?:fondotinta|mascara|rossetto|smalto|unghie|manicure|makeup|make up)\b",
            r"\b(?:shampoo|crema|profumo|rasoio|acido ialuronico|incarnato)\b",
            r"\b(?:l oreal|oreal|elvive|gillette|oral b|braun|remington)\b",
        ),
    ),
    (
        "musica",
        (
            r"\b(?:bacchette|drumsticks|vic firth|hickory|percussioni)\b",
            r"\b(?:chitarra|pianoforte|accordatore)\b",
        ),
    ),
    (
        "giochi",
        (
            r"\b(?:lego|giocattolo|giocattoli|gormiti|super mario|videogioco|videogiochi)\b",
            r"\b(?:pokemon|barbie|playmobil|hot wheels|bambola|bambole|peluche)\b",
        ),
    ),
    (
        "casa",
        (
            r"\b(?:barbecue|weber|friggitrice|aspirapolvere|detersivo|ammorbidente)\b",
            r"\b(?:cappa|filtri cappa|lampadina|lampadine|lampada)\b",
            r"\b(?:scarpiera|zanzariera|porta carta igienica|portarotolo)\b",
            r"\b(?:cornice in legno|vero vetro cornice|cornice foto|cornice da)\b",
            r"\b(?:materasso|lenzuola|cuscino|coperta|tappeto)\b",
        ),
    ),
    (
        "fai-da-te",
        (
            r"\b(?:trapano|avvitatore|paranco|puleggia|argano|smerigliatrice|idropulitrice)\b",
            r"\b(?:vite|viti|tasselli|bulloni|fischer|forgefix|porta inserti|wolfcraft)\b",
            r"\b(?:motosega|catena di ricambio|tagliaerba|seghetto|chiave regolabile)\b",
            r"\b(?:avvolgicavo|spina schuko|presa multipla|vimar|electraline)\b",
            r"\b(?:lucchetto a chiave|casco di sicurezza)\b",
        ),
    ),
    (
        "elettronica",
        (
            r"\b(?:smartphone|iphone|galaxy|tablet android|monitor|ssd|hard disk|microsd)\b",
            r"\b(?:router|wi fi|wifi|bluetooth|cuffie|auricolari|soundbar|speaker|altoparlanti)\b",
            r"\b(?:tv|televisore|fotocamera|telecamera|drone|proiettore|projector)\b",
            r"\b(?:powerbank|caricatore|tastiera|keyboard|mouse|microfono|stampante)\b",
            r"\b(?:smartwatch|monitoraggio .{0,30} sonno|monitoraggio .{0,30} salute)\b",
            r"\b(?:dissipatore(?: .{0,30})? cpu|raffreddatore(?: .{0,30})? cpu|aio cpu cooler|alimentatore atx)\b",
            r"\b(?:adattatore usb|antenna wifi|tapo|tp link|blink|stream deck|controller da studio)\b",
            r"\b(?:cavo patch|cavo antenna|cavo telefonico|coassiale|ethernet|rj45|rj11|dolby|hdr|oled|qled|mini led)\b",
            r"\b(?:batterie a moneta|batteria al litio|cr2032|ventole argb|argb)\b",
        ),
    ),
)

OFFER_SOURCE_KEYWORDS = (
    "offerte",
    "offerta",
    "sconti",
    "sconto",
    "coupon",
    "codici sconto",
    "deal",
    "deals",
    "prezzo",
    "amazon",
    "shopping",
    "promo",
    "promozioni",
    "risparmio",
    "bottega",
    "shark",
    "junction",
    "gizchina",
    "mercatino",
)

NEWS_SOURCE_KEYWORDS = (
    "news",
    "notizie",
    "breaking",
    "ansa",
    "ultimora",
    "giornale",
    "quotidiano",
    "informazione",
)


@dataclass(frozen=True)
class OfferFacts:
    category: str
    price: Decimal | None
    invalid: bool
    product: str | None = None
    original_price: Decimal | None = None
    offer_url: str | None = None

    @property
    def current_price(self) -> Decimal | None:
        return self.price

    @property
    def complete(self) -> bool:
        return all(
            (
                self.product,
                self.original_price is not None,
                self.current_price is not None,
                self.offer_url,
            )
        )


def _normalize_decimal(value: str) -> Decimal | None:
    cleaned = value.replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def extract_prices(text: str) -> list[Decimal]:
    prices = []
    for match in PRICE_RE.finditer(text):
        raw = match.group(0).lower()
        if "\u20ac" not in raw and "eur" not in raw and "euro" not in raw:
            continue
        price = _normalize_decimal(match.group(1))
        if price is not None and price > 0:
            prices.append(price)
    return prices


def extract_price(text: str) -> Decimal | None:
    prices = extract_prices(text)
    return min(prices) if prices else None


def extract_price_pair(text: str) -> tuple[Decimal | None, Decimal | None]:
    prices = extract_prices(text)
    unique_prices: list[Decimal] = []
    for price in prices:
        if price not in unique_prices:
            unique_prices.append(price)
    if len(unique_prices) < 2:
        return None, unique_prices[0] if unique_prices else None
    return max(unique_prices), min(unique_prices)


def known_filter_categories() -> tuple[str, ...]:
    return tuple(sorted({*CATEGORY_KEYWORDS.keys(), *BOOK_GENRE_KEYWORDS.keys()}))


@lru_cache(maxsize=4096)
def keyword_pattern(keyword: str) -> re.Pattern[str]:
    word_chars = "a-z0-9àèéìòùáíóúü"
    return re.compile(
        rf"(?<![{word_chars}]){re.escape(keyword.lower())}(?![{word_chars}])",
        flags=re.IGNORECASE,
    )


def keyword_score(text: str, keywords: tuple[str, ...]) -> int:
    return sum(1 for keyword in keywords if keyword_pattern(keyword).search(text))


def best_keyword_category(
    text: str,
    keyword_map: dict[str, tuple[str, ...]],
) -> tuple[str, int]:
    best_category = "altro"
    best_score = 0
    for category, keywords in keyword_map.items():
        score = keyword_score(text, keywords)
        if score > best_score:
            best_category = category
            best_score = score
    return best_category, best_score


def classify_book_genre(site_text: str, product_text: str) -> str | None:
    site_category, site_score = best_keyword_category(site_text, BOOK_GENRE_KEYWORDS)
    if site_score:
        return site_category
    product_category, product_score = best_keyword_category(product_text, BOOK_GENRE_KEYWORDS)
    if product_score >= 2:
        return product_category
    return None


def render_category(category: str, site_text: str, product_text: str) -> str:
    if category == "libri":
        return classify_book_genre(site_text, product_text) or "libri"
    return category


def product_category_should_override_site(
    site_category: str,
    site_score: int,
    product_category: str,
    product_score: int,
) -> bool:
    if not product_score or not site_score:
        return False
    if product_category == site_category:
        return False
    if product_category == "libri" and product_score >= site_score:
        return True
    misleading_site_categories = {
        "giochi": {"moda", "infanzia", "casa", "auto", "viaggi", "animali", "musica"},
        "elettronica": {
            "accessori",
            "giochi",
            "moda",
            "bellezza",
            "infanzia",
            "casa",
            "auto",
            "viaggi",
            "fai-da-te",
            "ufficio",
            "libri",
            "alimentari",
            "animali",
            "musica",
        },
        "fai-da-te": {
            "accessori",
            "animali",
            "bellezza",
            "casa",
            "elettronica",
            "giochi",
            "infanzia",
            "moda",
            "musica",
            "viaggi",
        },
        "casa": {"accessori", "animali", "bellezza", "elettronica", "infanzia", "moda", "viaggi"},
        "accessori": {"bellezza", "casa", "elettronica", "giochi", "infanzia", "moda", "viaggi"},
    }
    if product_category not in misleading_site_categories.get(site_category, set()):
        return False
    if site_category in {"elettronica", "giochi"}:
        return True
    return product_score >= max(2, site_score)


def high_confidence_product_category(text: str) -> str | None:
    normalized = normalize_text(text)
    if not normalized:
        return None
    for category, patterns in HIGH_CONFIDENCE_CATEGORY_RULES:
        if any(re.search(pattern, normalized) for pattern in patterns):
            return category
    return None


def classify_category(text: str, site_text: str = "") -> str:
    normalized_text = text.lower()
    normalized_site = site_text.lower()
    strong_product_category = high_confidence_product_category(text)
    if strong_product_category is not None:
        return render_category(strong_product_category, normalized_site, normalized_text)

    site_category, site_score = best_keyword_category(normalized_site, SITE_CATEGORY_KEYWORDS)
    product_category, product_score = best_keyword_category(normalized_text, CATEGORY_KEYWORDS)
    if product_category_should_override_site(site_category, site_score, product_category, product_score):
        return render_category(product_category, normalized_site, normalized_text)
    if site_category == "libri" and site_score:
        return render_category("libri", normalized_site, normalized_text)
    if site_score:
        return site_category

    if product_score:
        return render_category(product_category, normalized_site, normalized_text)
    return fallback_category(normalized_text, normalized_site)


def fallback_category(text: str, site_text: str = "") -> str:
    combined = f"{site_text} {text}".strip()
    if not combined:
        return "casa"
    if re.search(
        r"\b(?:mah|usb|hdmi|hz|gb|tb|bluetooth|wifi|wi fi|4k|5g|oled|qled|hdr|dolby|rgb|ips|lcd|led)\b",
        combined,
    ):
        return "elettronica"
    if re.search(r"\b(?:gusto|ingredienti|senza zucchero|proteine)\b", combined):
        return "alimentari"
    if re.search(r"\b\d+(?:[,.]\d+)?\s*(?:ml|litri|kg|gr)\b", combined):
        return "alimentari"
    if re.search(r"\b(?:cm|cotone|polipropilene|acciaio|legno|ceramica|vetro|bagno|camera|filtro|filtri)\b", combined):
        return "casa"
    if re.search(r"\b(?:uomo|donna|unisex|taglia|vestibilita|calzata)\b", combined):
        return "moda"
    return "casa"


def is_invalid_offer(text: str) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in INVALID_PATTERNS)


def is_offer_url(url: str) -> bool:
    lowered = url.lower()
    if "t.me/" in lowered or "telegram.me/" in lowered:
        return False
    if lowered.startswith("tg:"):
        return False
    if any(host in lowered for host in MEDIA_URL_HOSTS):
        return False
    if lowered.split("?", 1)[0].endswith(MEDIA_URL_EXTENSIONS):
        return False
    return lowered.startswith("http://") or lowered.startswith("https://")


def offer_url_priority(url: str) -> int:
    lowered = url.lower()
    if "amazon." in lowered and "/dp/" in canonicalize_url(url):
        return 0
    if any(host in lowered for host in ("amzlink.to", "amzn.to", "ofclub.click")):
        return 1
    return 2


def pick_offer_url(text: str, urls: tuple[str, ...] = ()) -> str | None:
    candidates = [*urls, *extract_urls(text)]
    offer_urls = [url for url in candidates if is_offer_url(url)]
    for url in sorted(offer_urls, key=offer_url_priority):
        return canonicalize_url(url)
    return None


def strip_noise(text: str) -> str:
    without_urls = re.sub(r"https?://\S+", " ", text)
    without_prices = PRICE_RE.sub(" ", without_urls)
    cleaned = re.sub(r"[^\w\s.,:/+-]", " ", without_prices, flags=re.UNICODE)
    return re.sub(r"\s+", " ", cleaned).strip()


def is_generic_product_candidate(candidate: str) -> bool:
    normalized = re.sub(r"\s+", " ", candidate.lower()).strip(" -:,.")
    return any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in GENERIC_PRODUCT_PATTERNS)


def clean_site_product_candidate(candidate: str) -> str:
    cleaned = re.sub(r"https?://\S+", " ", candidate)
    cleaned = re.sub(r"[^\w\s.,:/+-]", " ", cleaned, flags=re.UNICODE)
    cleaned = re.sub(r"\s*:\s*amazon\.[a-z.]+.*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -:,.")
    return cleaned


def site_product_from_context(site_text: str) -> str | None:
    skipped = {
        "dp",
        "gp",
        "amazon",
        "amazon.it",
        "giochi",
        "videogiochi",
        "elettronica",
        "casa",
        "libri",
    }
    for raw_part in site_text.split("|"):
        candidate = clean_site_product_candidate(raw_part)
        lowered = candidate.lower()
        if len(candidate) < 8:
            continue
        if lowered in skipped:
            continue
        if "›" in candidate:
            continue
        if is_generic_product_candidate(candidate):
            continue
        return candidate[:140].strip(" -:,.") or None
    return None


def extract_product(text: str, site_text: str = "") -> str | None:
    banned_fragments = (
        "iscriviti",
        "gratis",
        "canale",
        "telegram",
        "fonte",
        "coupon",
        "codice",
        "prezzo",
        "amazon",
        "link",
        "categoria",
    )
    best_line = ""
    for line in text.splitlines():
        candidate = strip_noise(line)
        lowered = candidate.lower()
        if len(candidate) < 8:
            continue
        if any(fragment in lowered for fragment in banned_fragments):
            continue
        if is_generic_product_candidate(candidate):
            continue
        if len(candidate) > len(best_line):
            best_line = candidate

    if not best_line:
        site_product = site_product_from_context(site_text)
        if site_product:
            return site_product
        candidate = strip_noise(text)
        if len(candidate) < 8 or is_generic_product_candidate(candidate):
            return None
        best_line = candidate

    return best_line[:140].strip(" -:,.") or None


def analyze_offer(text: str, urls: tuple[str, ...] = (), site_text: str = "") -> OfferFacts:
    original_price, current_price = extract_price_pair(text)
    return OfferFacts(
        category=classify_category(text, site_text),
        price=current_price,
        invalid=is_invalid_offer(text),
        product=extract_product(text, site_text),
        original_price=original_price,
        offer_url=pick_offer_url(text, urls),
    )


def source_score(
    title: str,
    username: str,
    mode: str = "offerte",
    source_type: str = "chat",
) -> int:
    haystack = f"{title} {username} {source_type}".lower()
    keywords = NEWS_SOURCE_KEYWORDS if mode == "notizie" else OFFER_SOURCE_KEYWORDS
    score = sum(2 for keyword in keywords if keyword in haystack)
    if username.lower().endswith("bot"):
        score += 1
    if source_type in {"channel", "bot"}:
        score += 1
    return score


def parse_price_limit(value: str) -> Decimal | None:
    return _normalize_decimal(value.strip().replace("\u20ac", "").replace("eur", ""))
