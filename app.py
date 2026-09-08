import hashlib
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from supabase import create_client


# ============================================================
# CONFIGURAZIONE
# ============================================================

SOURCES = {
    "Forlì": "https://forli.bakecaincontrii.com/donna-cerca-uomo/",
    "Rimini": "https://rimini.bakecaincontrii.com/donna-cerca-uomo/",
    "Ravenna": "https://ravenna.bakecaincontrii.com/donna-cerca-uomo/",
}

ADS_TABLE = "annunci"
PHONE_TABLE = "telefoni_registry"

REQUEST_TIMEOUT = 20

# Prime 10 pagine per ogni città
MAX_PAGES_PER_CITY = 10

# Piccole pause per non sovraccaricare i servizi
LISTING_PAGE_DELAY = 0.20
DETAIL_PAGE_DELAY = 0.25
SERPER_DELAY = 0.15


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0 Safari/537.36"
    ),
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.7",
}


# ============================================================
# UTILITA
# ============================================================

def utc_now_iso():
    return datetime.now(
        timezone.utc
    ).isoformat()


def format_datetime(value):
    if not value:
        return "-"

    try:
        dt = datetime.fromisoformat(
            str(value).replace(
                "Z",
                "+00:00"
            )
        )

        return dt.strftime(
            "%d/%m/%Y %H:%M"
        )

    except Exception:
        return str(value)


def clean_text(value):
    if not value:
        return ""

    return re.sub(
        r"\s+",
        " ",
        value
    ).strip()


def clean_domain(value):
    if not value:
        return ""

    value = str(
        value
    ).strip().lower()

    if "://" not in value:
        value = (
            "https://"
            + value
        )

    hostname = (
        urlparse(
            value
        ).hostname
        or ""
    )

    return hostname.lower().strip(".")


# ============================================================
# TELEFONO
# ============================================================

def normalize_phone(value):
    if not value:
        return ""

    value = str(
        value
    ).replace(
        "tel:",
        ""
    )

    digits = re.sub(
        r"\D",
        "",
        value
    )

    # 0039xxxxxxxxxx
    if digits.startswith(
        "0039"
    ):
        digits = digits[4:]

    # 39xxxxxxxxxx
    if (
        digits.startswith("39")
        and len(digits) >= 12
    ):
        digits = digits[2:]

    if not (
        8 <= len(digits) <= 11
    ):
        return ""

    return digits


def make_phone_hash(phone):
    normalized = normalize_phone(
        phone
    )

    if not normalized:
        return ""

    return hashlib.sha256(
        normalized.encode(
            "utf-8"
        )
    ).hexdigest()


def extract_phone_from_detail(html):
    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    candidates = []

    # --------------------------------------------------------
    # href="tel:"
    # --------------------------------------------------------

    for a in soup.select(
        'a[href^="tel:"]'
    ):
        candidates.append(
            a.get(
                "href",
                ""
            )
        )

    # --------------------------------------------------------
    # Attributi HTML
    # --------------------------------------------------------

    phone_attrs = (
        "data-phone",
        "data-telephone",
        "data-tel",
        "data-number",
        "data-phone-number",
        "phone",
        "telephone",
    )

    for tag in soup.find_all(
        True
    ):
        for attr in phone_attrs:

            if tag.has_attr(
                attr
            ):
                candidates.append(
                    str(
                        tag.get(
                            attr
                        )
                    )
                )

    # --------------------------------------------------------
    # Titolo pagina
    # --------------------------------------------------------

    if soup.title:
        candidates.append(
            soup.title.get_text(
                " ",
                strip=True
            )
        )

    # --------------------------------------------------------
    # Meta
    # --------------------------------------------------------

    for meta in soup.find_all(
        "meta"
    ):
        content = meta.get(
            "content"
        )

        if content:
            candidates.append(
                content
            )

    # --------------------------------------------------------
    # Script
    # --------------------------------------------------------

    for script in soup.find_all(
        "script"
    ):
        text = (
            script.string
            or script.get_text(
                " ",
                strip=True
            )
        )

        if text:
            candidates.append(
                text
            )

    # --------------------------------------------------------
    # Testo visibile
    # --------------------------------------------------------

    candidates.append(
        soup.get_text(
            " ",
            strip=True
        )
    )

    # --------------------------------------------------------
    # Cellulare italiano
    # --------------------------------------------------------

    mobile_pattern = re.compile(
        r"(?<!\d)"
        r"(?:\+?39[\s.\-]*)?"
        r"(3(?:[\s.\-]*\d){8,9})"
        r"(?!\d)"
    )

    for candidate in candidates:

        if not candidate:
            continue

        match = (
            mobile_pattern.search(
                candidate
            )
        )

        if match:

            phone = normalize_phone(
                match.group(0)
            )

            if phone:
                return phone

    return ""


# ============================================================
# RICONOSCIMENTO LINK ANNUNCI
# ============================================================

def looks_like_ad_url(href):
    if not href:
        return False

    absolute = urljoin(
        "https://www.bakecaincontrii.com/",
        href
    )

    parsed = urlparse(
        absolute
    )

    path = (
        parsed.path.lower()
    )

    if "/annuncio/" in path:
        return True

    blocked_parts = (
        "/donna-cerca-uomo/",
        "/uomo-cerca-uomo/",
        "/trans/",
        "/incontri/",
        "/pubblica",
        "/privacy",
        "/cookie",
        "/termin",
        "/contatt",
        "/assistenza",
    )

    return (
        parsed.netloc.endswith(
            "bakecaincontrii.com"
        )
        and not any(
            part in path
            for part in blocked_parts
        )
        and len(
            path.strip("/")
        ) > 20
    )


# ============================================================
# ESTRAZIONE ANNUNCI DA UNA PAGINA ELENCO
# ============================================================

def extract_listing_links(
    html,
    page_url
):
    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    results = []
    seen_urls = set()

    for a in soup.find_all(
        "a",
        href=True
    ):
        href = a.get(
            "href"
        )

        if not looks_like_ad_url(
            href
        ):
            continue

        url = urljoin(
            page_url,
            href
        )

        if url in seen_urls:
            continue

        title = clean_text(
            a.get_text(
                " ",
                strip=True
            )
        )

        # ----------------------------------------------------
        # ALT immagine
        # ----------------------------------------------------

        if len(title) < 5:

            img = a.find(
                "img",
                alt=True
            )

            if img:
                title = clean_text(
                    img.get(
                        "alt"
                    )
                )

        # ----------------------------------------------------
        # Heading vicino
        # ----------------------------------------------------

        if (
            len(title) < 5
            and a.parent
        ):

            heading = a.parent.find(
                [
                    "h1",
                    "h2",
                    "h3",
                    "h4"
                ]
            )

            if heading:

                title = clean_text(
                    heading.get_text(
                        " ",
                        strip=True
                    )
                )

        if len(title) < 5:
            continue

        seen_urls.add(
            url
        )

        results.append(
            {
                "titolo":
                    title,

                "url":
                    url,
            }
        )

    return results


# ============================================================
# HTTP
# ============================================================

def get_page(
    session,
    url
):
    response = session.get(
        url,
        timeout=REQUEST_TIMEOUT
    )

    response.raise_for_status()

    return response.text


# ============================================================
# SERPER
# ============================================================

def serper_domain_matches(
    phone,
    target_domain,
    max_results=20
):
    if not phone:
        return 0, []

    if (
        "SERPER_API_KEY"
        not in st.secrets
    ):
        raise RuntimeError(
            "SERPER_API_KEY non configurata "
            "nei Secrets di Streamlit."
        )

    target_domain = clean_domain(
        target_domain
    )

    if not target_domain:
        raise RuntimeError(
            "TARGET_DOMAIN non valido."
        )

    response = requests.post(
        "https://google.serper.dev/search",
        headers={
            "X-API-KEY":
                st.secrets[
                    "SERPER_API_KEY"
                ],

            "Content-Type":
                "application/json",
        },
        json={
            "q":
                str(phone),

            "num":
                min(
                    int(max_results),
                    20
                ),

            "gl":
                "it",

            "hl":
                "it",
        },
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    organic = (
        response.json()
        .get(
            "organic",
            []
        )[:max_results]
    )

    links = []
    seen = set()

    for item in organic:

        link = item.get(
            "link",
            ""
        )

        if not link:
            continue

        hostname = (
            urlparse(
                link
            ).hostname
            or ""
        ).lower()

        if (
            hostname
            == target_domain

            or hostname.endswith(
                "."
                + target_domain
            )
        ):

            if link not in seen:

                seen.add(
                    link
                )

                links.append(
                    link
                )

    return (
        len(links),
        links
    )


# ============================================================
# SUPABASE
# ============================================================

@st.cache_resource
def get_supabase():

    return create_client(
        st.secrets[
            "SUPABASE_URL"
        ],

        st.secrets[
            "SUPABASE_KEY"
        ],
    )


# ============================================================
# DATABASE ANNUNCI
# ============================================================

def load_all_ads():

    response = (
        get_supabase()
        .table(
            ADS_TABLE
        )
        .select("*")
        .order(
            "first_seen",
            desc=True
        )
        .execute()
    )

    return (
        response.data
        or []
    )


def load_known_ads():

    response = (
        get_supabase()
        .table(
            ADS_TABLE
        )
        .select(
            "url,telefono,phone_hash"
        )
        .execute()
    )

    result = {}

    for row in (
        response.data
        or []
    ):

        url = row.get(
            "url"
        )

        if url:

            result[
                url
            ] = {

                "telefono":
                    row.get(
                        "telefono"
                    )
                    or "",

                "phone_hash":
                    row.get(
                        "phone_hash"
                    )
                    or "",
            }

    return result


def insert_new_ad(row):

    (
        get_supabase()
        .table(
            ADS_TABLE
        )
        .insert(
            row
        )
        .execute()
    )


def update_last_seen(
    url,
    seen_at
):

    (
        get_supabase()
        .table(
            ADS_TABLE
        )
        .update(
            {
                "last_seen":
                    seen_at,

                "active":
                    True,
            }
        )
        .eq(
            "url",
            url
        )
        .execute()
    )


def mark_missing_as_inactive(
    current_urls
):
    """
    Segna inattivi gli URL che non compaiono
    nelle prime 10 pagine delle tre città.
    """

    db = get_supabase()

    for row in load_all_ads():

        url = row.get(
            "url"
        )

        if (
            url
            and url not in current_urls
            and row.get(
                "active",
                True
            )
        ):

            (
                db
                .table(
                    ADS_TABLE
                )
                .update(
                    {
                        "active":
                            False
                    }
                )
                .eq(
                    "url",
                    url
                )
                .execute()
            )


# ============================================================
# REGISTRO TELEFONI
# ============================================================

def load_phone_registry():

    response = (
        get_supabase()
        .table(
            PHONE_TABLE
        )
        .select("*")
        .execute()
    )

    return {

        row[
            "phone_hash"
        ]:
            row

        for row in (
            response.data
            or []
        )

        if row.get(
            "phone_hash"
        )
    }


def insert_phone_registry(
    phone_hash,
    seen_at
):

    (
        get_supabase()
        .table(
            PHONE_TABLE
        )
        .insert(
            {
                "phone_hash":
                    phone_hash,

                "first_seen":
                    seen_at,

                "last_seen":
                    seen_at,

                "processed":
                    False,

                "target_domain":
                    None,

                "match_count":
                    0,

                "match_urls":
                    [],

                "processed_at":
                    None,

                "note":
                    "",

                "hidden":
                    False,

                "hidden_at":
                    None,
            }
        )
        .execute()
    )


def update_phone_last_seen(
    phone_hash,
    seen_at
):

    if not phone_hash:
        return

    (
        get_supabase()
        .table(
            PHONE_TABLE
        )
        .update(
            {
                "last_seen":
                    seen_at
            }
        )
        .eq(
            "phone_hash",
            phone_hash
        )
        .execute()
    )


# ============================================================
# NOTE
# ============================================================

def save_phone_note(
    phone_hash,
    note
):

    (
        get_supabase()
        .table(
            PHONE_TABLE
        )
        .update(
            {
                "note":
                    note
                    or ""
            }
        )
        .eq(
            "phone_hash",
            phone_hash
        )
        .execute()
    )


# ============================================================
# NASCONDI / RIPRISTINA
# ============================================================

def set_phone_hidden(
    phone_hash,
    hidden
):

    payload = {

        "hidden":
            bool(hidden),

        "hidden_at":
            (
                utc_now_iso()
                if hidden
                else None
            ),
    }

    (
        get_supabase()
        .table(
            PHONE_TABLE
        )
        .update(
            payload
        )
        .eq(
            "phone_hash",
            phone_hash
        )
        .execute()
    )


# ============================================================
# CACHE SERPER
# ============================================================

def save_external_check(
    phone_hash,
    target_domain,
    match_count,
    match_urls
):

    (
        get_supabase()
        .table(
            PHONE_TABLE
        )
        .update(
            {
                "processed":
                    True,

                "target_domain":
                    clean_domain(
                        target_domain
                    ),

                "match_count":
                    int(
                        match_count
                    ),

                "match_urls":
                    match_urls,

                "processed_at":
                    utc_now_iso(),
            }
        )
        .eq(
            "phone_hash",
            phone_hash
        )
        .execute()
    )


def needs_external_check(
    registry_row,
    target_domain
):

    if not registry_row:
        return True

    # --------------------------------------------------------
    # I nascosti non vengono più verificati
    # --------------------------------------------------------

    if registry_row.get(
        "hidden",
        False
    ):
        return False

    # --------------------------------------------------------
    # Mai processato
    # --------------------------------------------------------

    if not registry_row.get(
        "processed",
        False
    ):
        return True

    stored_domain = clean_domain(
        registry_row.get(
            "target_domain",
            ""
        )
    )

    current_domain = clean_domain(
        target_domain
    )

    # --------------------------------------------------------
    # Se cambia dominio rifacciamo la ricerca una sola volta
    # --------------------------------------------------------

    return (
        stored_domain
        != current_domain
    )


def process_unchecked_phones(
    known_ads,
    phone_registry,
    target_domain
):

    query_count = 0
    errors = []

    phones_by_hash = {}

    # --------------------------------------------------------
    # Ricava un numero per ogni hash
    # --------------------------------------------------------

    for ad in known_ads.values():

        phone = normalize_phone(
            ad.get(
                "telefono",
                ""
            )
        )

        phone_hash = ad.get(
            "phone_hash",
            ""
        )

        if (
            phone
            and phone_hash
            and phone_hash
            not in phones_by_hash
        ):

            phones_by_hash[
                phone_hash
            ] = phone

    # --------------------------------------------------------
    # Una query massimo per phone_hash
    # --------------------------------------------------------

    for (
        phone_hash,
        phone
    ) in phones_by_hash.items():

        registry_row = (
            phone_registry.get(
                phone_hash
            )
        )

        if not needs_external_check(
            registry_row,
            target_domain
        ):
            continue

        try:

            (
                match_count,
                match_urls
            ) = serper_domain_matches(
                phone=phone,
                target_domain=target_domain,
                max_results=20,
            )

            save_external_check(
                phone_hash=phone_hash,
                target_domain=target_domain,
                match_count=match_count,
                match_urls=match_urls,
            )

            phone_registry[
                phone_hash
            ] = {

                **(
                    registry_row
                    or {}
                ),

                "phone_hash":
                    phone_hash,

                "processed":
                    True,

                "target_domain":
                    clean_domain(
                        target_domain
                    ),

                "match_count":
                    match_count,

                "match_urls":
                    match_urls,

                "processed_at":
                    utc_now_iso(),
            }

            query_count += 1

            time.sleep(
                SERPER_DELAY
            )

        except Exception as exc:

            errors.append(
                "Controllo esterno "
                f"{phone_hash[:8]}: "
                f"{exc}"
            )

    return (
        query_count,
        errors
    )


# ============================================================
# CREA URL PAGINA
# ============================================================

def build_listing_page_url(
    base_url,
    page_number
):
    """
    Pagina 1:
    https://.../donna-cerca-uomo/

    Pagina 2:
    https://.../donna-cerca-uomo/?p=2

    ecc.
    """

    if page_number <= 1:
        return base_url

    return (
        f"{base_url}"
        f"?p={page_number}"
    )


# ============================================================
# SCRAPING + SINCRONIZZAZIONE
# ============================================================

def scrape_and_sync(
    target_domain
):

    session = requests.Session()

    session.headers.update(
        HEADERS
    )

    known_ads = (
        load_known_ads()
    )

    phone_registry = (
        load_phone_registry()
    )

    current_urls = set()

    new_ads = 0
    known_ads_count = 0
    new_phones = 0
    duplicate_phones = 0

    pages_loaded = 0
    pages_failed = 0

    errors = []

    now = utc_now_iso()

    # ========================================================
    # CICLO CITTA
    # ========================================================

    for (
        city,
        source_url
    ) in SOURCES.items():

        city_listings = []
        seen_city_urls = set()

        # ====================================================
        # PRIME 10 PAGINE DELLA CITTA
        # ====================================================

        for page_number in range(
            1,
            MAX_PAGES_PER_CITY + 1
        ):

            page_url = (
                build_listing_page_url(
                    source_url,
                    page_number
                )
            )

            try:

                listing_html = get_page(
                    session,
                    page_url
                )

                page_listings = (
                    extract_listing_links(
                        listing_html,
                        page_url
                    )
                )

                pages_loaded += 1

            except requests.RequestException as exc:

                pages_failed += 1

                errors.append(
                    f"{city} "
                    f"pagina {page_number}: "
                    f"{exc}"
                )

                continue

            # ------------------------------------------------
            # Deduplica tra le 10 pagine
            # ------------------------------------------------

            for item in page_listings:

                if (
                    item["url"]
                    in seen_city_urls
                ):
                    continue

                seen_city_urls.add(
                    item["url"]
                )

                city_listings.append(
                    item
                )

            time.sleep(
                LISTING_PAGE_DELAY
            )

        # ====================================================
        # PROCESSA TUTTI GLI ANNUNCI TROVATI
        # ====================================================

        for item in city_listings:

            url = item[
                "url"
            ]

            current_urls.add(
                url
            )

            # =================================================
            # URL GIA CONOSCIUTO
            # =================================================

            if url in known_ads:

                known_ads_count += 1

                try:

                    update_last_seen(
                        url,
                        now
                    )

                    phone_hash = (
                        known_ads[
                            url
                        ]
                        .get(
                            "phone_hash",
                            ""
                        )
                    )

                    if phone_hash:

                        update_phone_last_seen(
                            phone_hash,
                            now
                        )

                except Exception as exc:

                    errors.append(
                        "Aggiornamento "
                        f"{url}: {exc}"
                    )

                # Non riapriamo il dettaglio
                continue

            # =================================================
            # NUOVO URL
            # =================================================

            phone = ""
            phone_hash = ""

            try:

                detail_html = get_page(
                    session,
                    url
                )

                phone = (
                    extract_phone_from_detail(
                        detail_html
                    )
                )

                phone_hash = (
                    make_phone_hash(
                        phone
                    )
                )

            except requests.RequestException as exc:

                errors.append(
                    "Dettaglio "
                    f"{url}: {exc}"
                )

            # =================================================
            # REGISTRO TELEFONO
            # =================================================

            if phone_hash:

                # ---------------------------------------------
                # Numero già visto
                # ---------------------------------------------

                if (
                    phone_hash
                    in phone_registry
                ):

                    duplicate_phones += 1

                    try:

                        update_phone_last_seen(
                            phone_hash,
                            now
                        )

                    except Exception as exc:

                        errors.append(
                            "Telefono esistente: "
                            f"{exc}"
                        )

                # ---------------------------------------------
                # Numero nuovo
                # ---------------------------------------------

                else:

                    try:

                        insert_phone_registry(
                            phone_hash,
                            now
                        )

                        phone_registry[
                            phone_hash
                        ] = {

                            "phone_hash":
                                phone_hash,

                            "first_seen":
                                now,

                            "last_seen":
                                now,

                            "processed":
                                False,

                            "target_domain":
                                None,

                            "match_count":
                                0,

                            "match_urls":
                                [],

                            "processed_at":
                                None,

                            "note":
                                "",

                            "hidden":
                                False,

                            "hidden_at":
                                None,
                        }

                        new_phones += 1

                    except Exception as exc:

                        errors.append(
                            "Nuovo telefono: "
                            f"{exc}"
                        )

            # =================================================
            # SALVA IL NUOVO ANNUNCIO
            # =================================================

            row = {

                "url":
                    url,

                "titolo":
                    item[
                        "titolo"
                    ],

                "citta":
                    city,

                "telefono":
                    phone
                    or "",

                "phone_hash":
                    phone_hash
                    or None,

                "first_seen":
                    now,

                "last_seen":
                    now,

                "active":
                    True,

                "note":
                    "",

                "external_check_done":
                    False,

                "external_match_count":
                    None,
            }

            try:

                insert_new_ad(
                    row
                )

                known_ads[
                    url
                ] = {

                    "telefono":
                        phone,

                    "phone_hash":
                        phone_hash,
                }

                new_ads += 1

            except Exception as exc:

                errors.append(
                    "Salvataggio annuncio "
                    f"{url}: {exc}"
                )

            time.sleep(
                DETAIL_PAGE_DELAY
            )

    # ========================================================
    # ANNUNCI INATTIVI
    # ========================================================

    # Lo facciamo soltanto se TUTTE le 30 pagine
    # sono state lette correttamente.
    #
    # In caso contrario evitiamo falsi "inattivi".
    expected_pages = (
        len(SOURCES)
        * MAX_PAGES_PER_CITY
    )

    if (
        current_urls
        and pages_failed == 0
        and pages_loaded
        == expected_pages
    ):

        try:

            mark_missing_as_inactive(
                current_urls
            )

        except Exception as exc:

            errors.append(
                "Aggiornamento inattivi: "
                f"{exc}"
            )

    elif pages_failed > 0:

        errors.append(
            "Almeno una pagina elenco non è "
            "stata letta correttamente: "
            "lo stato active degli annunci "
            "mancanti non è stato modificato."
        )

    # ========================================================
    # SERPER
    # ========================================================

    (
        serper_queries,
        serper_errors
    ) = process_unchecked_phones(
        known_ads=known_ads,
        phone_registry=phone_registry,
        target_domain=target_domain,
    )

    errors.extend(
        serper_errors
    )

    return {

        "new_ads":
            new_ads,

        "known_ads":
            known_ads_count,

        "new_phones":
            new_phones,

        "duplicate_phones":
            duplicate_phones,

        "current":
            len(
                current_urls
            ),

        "pages_loaded":
            pages_loaded,

        "pages_failed":
            pages_failed,

        "serper_queries":
            serper_queries,

        "errors":
            errors,
    }


# ============================================================
# VISTA RAGGRUPPATA
# ============================================================

def build_grouped_view(
    ads_rows,
    registry_rows,
    show_hidden=False
):

    registry = {

        row[
            "phone_hash"
        ]:
            row

        for row in registry_rows

        if row.get(
            "phone_hash"
        )
    }

    df = pd.DataFrame(
        ads_rows
    )

    if df.empty:
        return []

    if "active" not in df.columns:

        df[
            "active"
        ] = True

    # --------------------------------------------------------
    # Solo annunci attivi
    # --------------------------------------------------------

    df = df[
        df[
            "active"
        ] == True
    ].copy()

    # --------------------------------------------------------
    # Solo annunci con phone_hash
    # --------------------------------------------------------

    df = df[
        df[
            "phone_hash"
        ]
        .fillna("")
        .astype(str)
        .str.len()
        > 0
    ]

    groups = []

    for (
        phone_hash,
        group
    ) in df.groupby(
        "phone_hash"
    ):

        reg = registry.get(
            phone_hash,
            {}
        )

        hidden = bool(
            reg.get(
                "hidden",
                False
            )
        )

        if (
            hidden
            and not show_hidden
        ):
            continue

        # ----------------------------------------------------
        # Città
        # ----------------------------------------------------

        cities = sorted(
            {
                str(city)

                for city in (
                    group[
                        "citta"
                    ]
                    .dropna()
                )

                if str(
                    city
                ).strip()
            }
        )

        # ----------------------------------------------------
        # Annunci
        # ----------------------------------------------------

        ads = []

        for _, row in (
            group.iterrows()
        ):

            ads.append(
                {
                    "titolo":
                        row.get(
                            "titolo"
                        )
                        or "Annuncio",

                    "url":
                        row.get(
                            "url"
                        )
                        or "",

                    "citta":
                        row.get(
                            "citta"
                        )
                        or "",
                }
            )

        # ----------------------------------------------------
        # Link esterni
        # ----------------------------------------------------

        match_urls = (
            reg.get(
                "match_urls"
            )
            or []
        )

        if not isinstance(
            match_urls,
            list
        ):

            match_urls = []

        groups.append(
            {
                "telefono_id":
                    phone_hash[:8],

                "phone_hash":
                    phone_hash,

                "first_seen":
                    reg.get(
                        "first_seen"
                    ),

                "last_seen":
                    reg.get(
                        "last_seen"
                    ),

                "citta":
                    ", ".join(
                        cities
                    ),

                "annunci":
                    ads,

                "numero_annunci":
                    len(
                        ads
                    ),

                "match_count":
                    int(
                        reg.get(
                            "match_count"
                        )
                        or 0
                    ),

                "match_urls":
                    match_urls,

                "processed":
                    bool(
                        reg.get(
                            "processed",
                            False
                        )
                    ),

                "note":
                    reg.get(
                        "note"
                    )
                    or "",

                "hidden":
                    hidden,

                "hidden_at":
                    reg.get(
                        "hidden_at"
                    ),
            }
        )

    # ========================================================
    # ORDINAMENTO
    #
    # 1. Match decrescente
    # 2. Numero annunci decrescente
    # 3. First seen più recente
    # ========================================================

    groups.sort(
        key=lambda x: (
            x[
                "match_count"
            ],

            x[
                "numero_annunci"
            ],

            str(
                x[
                    "first_seen"
                ]
                or ""
            ),
        ),
        reverse=True,
    )

    return groups


# ============================================================
# STREAMLIT
# ============================================================

st.set_page_config(
    page_title=(
        "Archivio annunci Romagna"
    ),
    page_icon="🔎",
    layout="wide",
)


# ============================================================
# CSS
# ============================================================

st.markdown(
    """
    <style>

    /* Riduce un po' gli spazi verticali */
    .block-container {
        padding-top: 1.5rem;
        padding-bottom: 2rem;
    }

    /* I container con bordo diventano le nostre righe/celle */
    div[data-testid="stVerticalBlockBorderWrapper"] {
        border-color: rgba(128, 128, 128, 0.45);
    }

    /* Testo dei pulsanti leggermente più compatto */
    div[data-testid="stButton"] button {
        min-height: 2.4rem;
    }

    </style>
    """,
    unsafe_allow_html=True,
)


st.title(
    "🔎 Archivio annunci "
    "Forlì, Rimini e Ravenna"
)


# ============================================================
# CONTROLLO SECRETS
# ============================================================

required_secrets = (
    "SUPABASE_URL",
    "SUPABASE_KEY",
    "SERPER_API_KEY",
    "TARGET_DOMAIN",
)


missing = [

    key

    for key in (
        required_secrets
    )

    if key not in (
        st.secrets
    )
]


if missing:

    st.error(
        "Mancano i Secrets "
        "Streamlit: "
        + ", ".join(
            missing
        )
    )

    st.stop()


TARGET_DOMAIN = clean_domain(
    st.secrets[
        "TARGET_DOMAIN"
    ]
)


if not TARGET_DOMAIN:

    st.error(
        "TARGET_DOMAIN non valido."
    )

    st.stop()


st.caption(
    f"Scraping: prime "
    f"{MAX_PAGES_PER_CITY} pagine "
    f"per ciascuna città. "
    f"Dominio esterno configurato: "
    f"{TARGET_DOMAIN}."
)


# ============================================================
# PULSANTE AGGIORNAMENTO
# ============================================================

if st.button(
    "🔄 Cerca nuovi annunci",
    type="primary"
):

    try:

        with st.spinner(
            "Controllo le prime 10 pagine "
            "di Forlì, Rimini e Ravenna..."
        ):

            result = (
                scrape_and_sync(
                    TARGET_DOMAIN
                )
            )

        st.success(
            f"Pagine lette: "
            f"{result['pages_loaded']}/"
            f"{len(SOURCES) * MAX_PAGES_PER_CITY} · "
            f"Nuovi annunci: "
            f"{result['new_ads']} · "
            f"Già conosciuti: "
            f"{result['known_ads']} · "
            f"Nuovi telefoni: "
            f"{result['new_phones']} · "
            f"Telefoni duplicati: "
            f"{result['duplicate_phones']} · "
            f"Query Serper: "
            f"{result['serper_queries']}"
        )

        if result[
            "errors"
        ]:

            with st.expander(
                f"Dettagli / avvisi "
                f"({len(result['errors'])})"
            ):

                for error in (
                    result[
                        "errors"
                    ]
                ):

                    st.write(
                        error
                    )

    except Exception as exc:

        st.error(
            "Errore durante "
            "l'aggiornamento: "
            f"{exc}"
        )


# ============================================================
# CARICAMENTO DATI
# ============================================================

try:

    ads_rows = (
        load_all_ads()
    )

    registry_response = (
        get_supabase()
        .table(
            PHONE_TABLE
        )
        .select("*")
        .execute()
    )

    registry_rows = (
        registry_response.data
        or []
    )

except Exception as exc:

    st.error(
        "Impossibile leggere "
        "Supabase: "
        f"{exc}"
    )

    st.stop()


# ============================================================
# MOSTRA NASCOSTI
# ============================================================

show_hidden = st.checkbox(
    "👁 Mostra anche risultati nascosti",
    value=False
)


groups = build_grouped_view(
    ads_rows=ads_rows,
    registry_rows=registry_rows,
    show_hidden=show_hidden,
)


if not groups:

    st.info(
        "Nessun risultato da mostrare. "
        "Premi “Cerca nuovi annunci” "
        "oppure mostra i risultati nascosti."
    )

    st.stop()


# ============================================================
# METRICHE
# ============================================================

hidden_total = sum(

    1

    for row in registry_rows

    if row.get(
        "hidden",
        False
    )
)


m1, m2, m3, m4 = (
    st.columns(4)
)


m1.metric(
    "Telefoni mostrati",
    len(
        groups
    )
)


m2.metric(
    "Con almeno un match",
    sum(
        1

        for group in groups

        if group[
            "match_count"
        ] > 0
    )
)


m3.metric(
    "Match totali",
    sum(
        group[
            "match_count"
        ]

        for group in groups
    )
)


m4.metric(
    "Nascosti",
    hidden_total
)


# ============================================================
# TABELLA
# ============================================================

st.subheader(
    "Risultati"
)


st.caption(
    "Ordinati per numero di match "
    "dal più alto al più basso."
)


COLUMN_WIDTHS = [
    1.0,   # Telefono ID
    1.4,   # First seen
    1.1,   # Città
    3.2,   # Annunci
    0.7,   # Match
    2.4,   # Link esterni
    2.2,   # Note
    1.2,   # Azione
]


HEADERS_TABLE = [
    "Telefono ID",
    "First seen",
    "Città",
    "Annunci",
    "Match",
    "Link esterni",
    "Note",
    "Azione",
]


# ============================================================
# INTESTAZIONE CON BORDI
# ============================================================

with st.container(
    border=True
):

    header_cols = st.columns(
        COLUMN_WIDTHS,
        gap="small"
    )

    for (
        col,
        label
    ) in zip(
        header_cols,
        HEADERS_TABLE
    ):

        with col:

            with st.container(
                border=True
            ):

                st.markdown(
                    f"**{label}**"
                )


# ============================================================
# RIGHE CON BORDI
# ============================================================

for group in groups:

    phone_hash = group[
        "phone_hash"
    ]

    # --------------------------------------------------------
    # Bordo esterno della riga
    # --------------------------------------------------------

    with st.container(
        border=True
    ):

        cols = st.columns(
            COLUMN_WIDTHS,
            gap="small"
        )

        # ====================================================
        # TELEFONO ID
        # ====================================================

        with cols[0]:

            with st.container(
                border=True
            ):

                st.code(
                    group[
                        "telefono_id"
                    ],
                    language=None
                )

                if group[
                    "hidden"
                ]:

                    st.caption(
                        "🙈 Nascosto"
                    )

        # ====================================================
        # FIRST SEEN
        # ====================================================

        with cols[1]:

            with st.container(
                border=True
            ):

                st.write(
                    format_datetime(
                        group[
                            "first_seen"
                        ]
                    )
                )

        # ====================================================
        # CITTA
        # ====================================================

        with cols[2]:

            with st.container(
                border=True
            ):

                st.write(
                    group[
                        "citta"
                    ]
                    or "-"
                )

        # ====================================================
        # ANNUNCI
        # ====================================================

        with cols[3]:

            with st.container(
                border=True
            ):

                for (
                    index,
                    ad
                ) in enumerate(
                    group[
                        "annunci"
                    ],
                    start=1
                ):

                    title = (
                        ad[
                            "titolo"
                        ]
                        or
                        f"Annuncio {index}"
                    )

                    url = ad[
                        "url"
                    ]

                    if url:

                        st.markdown(
                            f"{index}. "
                            f"[{title}]({url})"
                        )

        # ====================================================
        # MATCH
        # ====================================================

        with cols[4]:

            with st.container(
                border=True
            ):

                if group[
                    "processed"
                ]:

                    st.markdown(
                        f"### "
                        f"{group['match_count']}"
                    )

                else:

                    st.write(
                        "—"
                    )

        # ====================================================
        # LINK ESTERNI
        # ====================================================

        with cols[5]:

            with st.container(
                border=True
            ):

                if group[
                    "match_urls"
                ]:

                    for (
                        index,
                        link
                    ) in enumerate(
                        group[
                            "match_urls"
                        ],
                        start=1
                    ):

                        st.markdown(
                            f"[Risultato "
                            f"{index}]({link})"
                        )

                elif group[
                    "processed"
                ]:

                    st.caption(
                        "Nessun risultato"
                    )

                else:

                    st.caption(
                        "Non verificato"
                    )

        # ====================================================
        # NOTE
        # ====================================================

        with cols[6]:

            with st.container(
                border=True
            ):

                note_value = (
                    st.text_area(
                        "Nota",
                        value=group[
                            "note"
                        ],
                        key=(
                            "note_"
                            + phone_hash
                        ),
                        label_visibility=(
                            "collapsed"
                        ),
                        height=90
                    )
                )

                if st.button(
                    "💾 Salva nota",
                    key=(
                        "save_note_"
                        + phone_hash
                    ),
                    use_container_width=True
                ):

                    try:

                        save_phone_note(
                            phone_hash,
                            note_value
                        )

                        st.success(
                            "Nota salvata"
                        )

                        st.rerun()

                    except Exception as exc:

                        st.error(
                            f"Errore: {exc}"
                        )

        # ====================================================
        # AZIONE
        # ====================================================

        with cols[7]:

            with st.container(
                border=True
            ):

                if group[
                    "hidden"
                ]:

                    if st.button(
                        "👁 Ripristina",
                        key=(
                            "restore_"
                            + phone_hash
                        ),
                        use_container_width=True
                    ):

                        try:

                            set_phone_hidden(
                                phone_hash,
                                False
                            )

                            st.rerun()

                        except Exception as exc:

                            st.error(
                                f"Errore: "
                                f"{exc}"
                            )

                else:

                    if st.button(
                        "🙈 Nascondi",
                        key=(
                            "hide_"
                            + phone_hash
                        ),
                        use_container_width=True
                    ):

                        try:

                            set_phone_hidden(
                                phone_hash,
                                True
                            )

                            st.rerun()

                        except Exception as exc:

                            st.error(
                                f"Errore: "
                                f"{exc}"
                            )


# ============================================================
# CSV
# ============================================================

csv_rows = []


for group in groups:

    csv_rows.append(
        {
            "telefono_id":
                group[
                    "telefono_id"
                ],

            "first_seen":
                group[
                    "first_seen"
                ],

            "citta":
                group[
                    "citta"
                ],

            "numero_annunci":
                group[
                    "numero_annunci"
                ],

            "match_count":
                group[
                    "match_count"
                ],

            "note":
                group[
                    "note"
                ],

            "hidden":
                group[
                    "hidden"
                ],

            "annunci":
                " | ".join(
                    (
                        f"{ad['titolo']} "
                        f"({ad['url']})"
                    )

                    for ad in (
                        group[
                            "annunci"
                        ]
                    )
                ),

            "match_urls":
                " | ".join(
                    group[
                        "match_urls"
                    ]
                ),
        }
    )


csv_data = (
    pd.DataFrame(
        csv_rows
    )
    .to_csv(
        index=False
    )
    .encode(
        "utf-8-sig"
    )
)


st.download_button(
    "📥 Scarica CSV",
    data=csv_data,
    file_name=(
        "annunci_raggruppati.csv"
    ),
    mime="text/csv"
)
