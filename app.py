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

MAX_PAGES_PER_CITY = 10

SCRAPING_TIMEOUT = 15
SERPER_TIMEOUT = 12

LISTING_PAGE_DELAY = 0.15
DETAIL_PAGE_DELAY = 0.20
SERPER_DELAY = 0.15

SUPABASE_PAGE_SIZE = 1000


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0 Safari/537.36"
    ),
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.7",
}


# ============================================================
# DEBUG
# ============================================================

def debug_enabled():

    value = str(
        st.secrets.get(
            "DEBUG",
            "false"
        )
    ).lower()

    return value in (
        "true",
        "1",
        "yes",
        "on",
    )


def add_debug_log(
    logs,
    message,
    log_box=None,
):

    if not debug_enabled():
        return

    timestamp = datetime.now().strftime(
        "%H:%M:%S"
    )

    line = (
        f"{timestamp} - {message}"
    )

    logs.append(
        line
    )

    if log_box is not None:

        log_box.code(
            "\n".join(
                logs[-300:]
            ),
            language=None,
        )


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

    digits = re.sub(
        r"\D",
        "",
        str(value).replace(
            "tel:",
            ""
        )
    )

    if digits.startswith(
        "0039"
    ):

        digits = digits[4:]

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
    # Link tel:
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

    for tag in soup.find_all(True):

        for attr in phone_attrs:

            if tag.has_attr(attr):

                candidates.append(
                    str(
                        tag.get(
                            attr
                        )
                    )
                )

    # --------------------------------------------------------
    # Titolo
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
    # Testo pagina
    # --------------------------------------------------------

    candidates.append(
        soup.get_text(
            " ",
            strip=True
        )
    )

    mobile_pattern = re.compile(
        r"(?<!\d)"
        r"(?:\+?39[\s.\-]*)?"
        r"(3(?:[\s.\-]*\d){8,9})"
        r"(?!\d)"
    )

    for candidate in candidates:

        if not candidate:
            continue

        match = mobile_pattern.search(
            candidate
        )

        if match:

            phone = normalize_phone(
                match.group(0)
            )

            if phone:

                return phone

    return ""


# ============================================================
# ANNUNCI
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

    path = parsed.path.lower()

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


def build_listing_page_url(
    base_url,
    page_number
):

    if page_number <= 1:

        return base_url

    return (
        f"{base_url}"
        f"?p={page_number}"
    )


# ============================================================
# HTTP
# ============================================================

def get_page(
    session,
    url,
    timeout=SCRAPING_TIMEOUT
):

    response = session.get(
        url,
        timeout=timeout,
    )

    response.raise_for_status()

    return response.text


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
# LETTURA PAGINATA SUPABASE
# ============================================================

def load_all_ads():

    db = get_supabase()

    all_rows = []

    start = 0

    while True:

        response = (
            db
            .table(
                ADS_TABLE
            )
            .select("*")
            .order(
                "first_seen",
                desc=True
            )
            .range(
                start,
                start
                + SUPABASE_PAGE_SIZE
                - 1
            )
            .execute()
        )

        rows = (
            response.data
            or []
        )

        all_rows.extend(
            rows
        )

        if len(rows) < SUPABASE_PAGE_SIZE:

            break

        start += SUPABASE_PAGE_SIZE

    return all_rows


def load_known_ads():

    db = get_supabase()

    result = {}

    start = 0

    while True:

        response = (
            db
            .table(
                ADS_TABLE
            )
            .select(
                "url,telefono,phone_hash"
            )
            .range(
                start,
                start
                + SUPABASE_PAGE_SIZE
                - 1
            )
            .execute()
        )

        rows = (
            response.data
            or []
        )

        for row in rows:

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

        if len(rows) < SUPABASE_PAGE_SIZE:

            break

        start += SUPABASE_PAGE_SIZE

    return result


def load_phone_registry():

    db = get_supabase()

    result = {}

    start = 0

    while True:

        response = (
            db
            .table(
                PHONE_TABLE
            )
            .select("*")
            .range(
                start,
                start
                + SUPABASE_PAGE_SIZE
                - 1
            )
            .execute()
        )

        rows = (
            response.data
            or []
        )

        for row in rows:

            phone_hash = row.get(
                "phone_hash"
            )

            if phone_hash:

                result[
                    phone_hash
                ] = row

        if len(rows) < SUPABASE_PAGE_SIZE:

            break

        start += SUPABASE_PAGE_SIZE

    return result


# ============================================================
# DATABASE ANNUNCI
# ============================================================

def insert_new_ad(row):

    try:

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

        return True

    except Exception as exc:

        error_text = str(
            exc
        )

        if (
            "23505" in error_text
            or
            "duplicate key"
            in error_text.lower()
            or
            "annunci_url_key"
            in error_text
        ):

            return False

        raise


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

                # Lo lasciamo TRUE per compatibilità,
                # ma NON viene più usato dalla GUI.
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


# ============================================================
# REGISTRO TELEFONI
# ============================================================

def insert_phone_registry(
    phone_hash,
    seen_at
):

    try:

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

        return True

    except Exception as exc:

        error_text = str(
            exc
        )

        if (
            "23505" in error_text
            or
            "duplicate key"
            in error_text.lower()
        ):

            return False

        raise


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

    (
        get_supabase()
        .table(
            PHONE_TABLE
        )
        .update(
            {
                "hidden":
                    bool(
                        hidden
                    ),

                "hidden_at":
                    (
                        utc_now_iso()
                        if hidden
                        else None
                    ),
            }
        )
        .eq(
            "phone_hash",
            phone_hash
        )
        .execute()
    )


# ============================================================
# FASE 1 - SCRAPING
# ============================================================

def scrape_ads_and_phones(
    progress_bar=None,
    status_box=None,
    log_box=None
):

    logs = []

    add_debug_log(
        logs,
        "INIZIO FASE SCRAPING",
        log_box,
    )

    session = requests.Session()

    session.headers.update(
        HEADERS
    )

    add_debug_log(
        logs,
        "Caricamento URL già presenti in Supabase...",
        log_box,
    )

    known_ads = (
        load_known_ads()
    )

    add_debug_log(
        logs,
        (
            "URL già presenti in Supabase: "
            f"{len(known_ads)}"
        ),
        log_box,
    )

    phone_registry = (
        load_phone_registry()
    )

    add_debug_log(
        logs,
        (
            "Telefoni già presenti nel registry: "
            f"{len(phone_registry)}"
        ),
        log_box,
    )

    new_ads = 0
    known_ads_count = 0
    duplicate_ads_count = 0
    new_phones = 0
    duplicate_phones = 0

    pages_loaded = 0
    pages_failed = 0

    errors = []

    now = utc_now_iso()

    total_pages = (
        len(SOURCES)
        * MAX_PAGES_PER_CITY
    )

    processed_pages = 0

    all_city_listings = {}

    # ========================================================
    # RACCOLTA URL DALLE 30 PAGINE
    # ========================================================

    for (
        city,
        source_url
    ) in SOURCES.items():

        add_debug_log(
            logs,
            f"Inizio città: {city}",
            log_box,
        )

        city_listings = []

        seen_city_urls = set()

        for page_number in range(
            1,
            MAX_PAGES_PER_CITY + 1
        ):

            processed_pages += 1

            page_url = (
                build_listing_page_url(
                    source_url,
                    page_number
                )
            )

            if status_box is not None:

                status_box.info(
                    f"Pagina "
                    f"{processed_pages}/{total_pages} "
                    f"— {city} "
                    f"pagina {page_number}"
                )

            add_debug_log(
                logs,
                (
                    f"GET elenco "
                    f"{city} "
                    f"pagina {page_number}: "
                    f"{page_url}"
                ),
                log_box,
            )

            try:

                html = get_page(
                    session,
                    page_url
                )

                page_listings = (
                    extract_listing_links(
                        html,
                        page_url
                    )
                )

                pages_loaded += 1

                add_debug_log(
                    logs,
                    (
                        f"{city} pagina "
                        f"{page_number}: "
                        f"{len(page_listings)} "
                        f"annunci trovati"
                    ),
                    log_box,
                )

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

            except Exception as exc:

                pages_failed += 1

                error = (
                    f"{city} pagina "
                    f"{page_number}: "
                    f"{exc}"
                )

                errors.append(
                    error
                )

                add_debug_log(
                    logs,
                    "ERRORE " + error,
                    log_box,
                )

            if progress_bar is not None:

                progress_bar.progress(
                    min(
                        processed_pages
                        / total_pages,
                        1.0
                    )
                )

            time.sleep(
                LISTING_PAGE_DELAY
            )

        all_city_listings[
            city
        ] = city_listings

        add_debug_log(
            logs,
            (
                f"{city}: "
                f"{len(city_listings)} "
                f"URL unici"
            ),
            log_box,
        )

    # ========================================================
    # PROCESSA GLI URL
    # ========================================================

    total_items = sum(
        len(items)
        for items
        in all_city_listings.values()
    )

    processed_items = 0

    if progress_bar is not None:

        progress_bar.progress(
            0
        )

    for (
        city,
        listings
    ) in all_city_listings.items():

        for item in listings:

            processed_items += 1

            url = item[
                "url"
            ]

            if status_box is not None:

                status_box.info(
                    f"Annuncio "
                    f"{processed_items}/{total_items} "
                    f"— {city}"
                )

            # =================================================
            # URL GIÀ CONOSCIUTO
            # =================================================

            if url in known_ads:

                known_ads_count += 1

                add_debug_log(
                    logs,
                    (
                        f"GIÀ CONOSCIUTO "
                        f"{url}"
                    ),
                    log_box,
                )

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

                    error = (
                        f"Aggiornamento "
                        f"{url}: {exc}"
                    )

                    errors.append(
                        error
                    )

                    add_debug_log(
                        logs,
                        "ERRORE " + error,
                        log_box,
                    )

            # =================================================
            # URL NUOVO
            # =================================================

            else:

                add_debug_log(
                    logs,
                    (
                        f"NUOVO URL: "
                        f"{url}"
                    ),
                    log_box,
                )

                phone = ""

                phone_hash = ""

                try:

                    add_debug_log(
                        logs,
                        (
                            f"Apro dettaglio: "
                            f"{url}"
                        ),
                        log_box,
                    )

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

                    if phone:

                        add_debug_log(
                            logs,
                            (
                                "Telefono trovato "
                                f"(hash {phone_hash[:8]})"
                            ),
                            log_box,
                        )

                    else:

                        add_debug_log(
                            logs,
                            "Telefono NON trovato",
                            log_box,
                        )

                except Exception as exc:

                    error = (
                        f"Dettaglio "
                        f"{url}: {exc}"
                    )

                    errors.append(
                        error
                    )

                    add_debug_log(
                        logs,
                        "ERRORE " + error,
                        log_box,
                    )

                # --------------------------------------------
                # REGISTRO TELEFONO
                # --------------------------------------------

                if phone_hash:

                    if (
                        phone_hash
                        in phone_registry
                    ):

                        duplicate_phones += 1

                        add_debug_log(
                            logs,
                            (
                                "Telefono già presente "
                                "nel registry "
                                f"{phone_hash[:8]}"
                            ),
                            log_box,
                        )

                        try:

                            update_phone_last_seen(
                                phone_hash,
                                now
                            )

                        except Exception as exc:

                            errors.append(
                                f"Telefono esistente: "
                                f"{exc}"
                            )

                    else:

                        try:

                            inserted_phone = (
                                insert_phone_registry(
                                    phone_hash,
                                    now
                                )
                            )

                            if inserted_phone:

                                new_phones += 1

                                add_debug_log(
                                    logs,
                                    (
                                        "Nuovo telefono "
                                        "salvato nel registry "
                                        f"{phone_hash[:8]}"
                                    ),
                                    log_box,
                                )

                            else:

                                duplicate_phones += 1

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

                        except Exception as exc:

                            error = (
                                "Nuovo telefono: "
                                f"{exc}"
                            )

                            errors.append(
                                error
                            )

                            add_debug_log(
                                logs,
                                "ERRORE " + error,
                                log_box,
                            )

                # --------------------------------------------
                # SALVA ANNUNCIO
                # --------------------------------------------

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

                    # Manteniamo il campo per compatibilità.
                    # La GUI però NON lo usa più.
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

                    inserted_ad = (
                        insert_new_ad(
                            row
                        )
                    )

                    if inserted_ad:

                        new_ads += 1

                        known_ads[
                            url
                        ] = {
                            "telefono":
                                phone,

                            "phone_hash":
                                phone_hash,
                        }

                        add_debug_log(
                            logs,
                            "Annuncio salvato in Supabase",
                            log_box,
                        )

                    else:

                        duplicate_ads_count += 1

                        add_debug_log(
                            logs,
                            (
                                "DUPLICATO rilevato "
                                "al salvataggio: "
                                f"{url}"
                            ),
                            log_box,
                        )

                except Exception as exc:

                    error = (
                        "Salvataggio annuncio "
                        f"{url}: {exc}"
                    )

                    errors.append(
                        error
                    )

                    add_debug_log(
                        logs,
                        "ERRORE " + error,
                        log_box,
                    )

                time.sleep(
                    DETAIL_PAGE_DELAY
                )

            if progress_bar is not None:

                progress_bar.progress(
                    min(
                        processed_items
                        / max(
                            total_items,
                            1
                        ),
                        1.0
                    )
                )

    # ========================================================
    # IMPORTANTE:
    # NON MARCARE PIÙ COME INATTIVI
    # ========================================================

    add_debug_log(
        logs,
        (
            "NON modifico active=False per gli annunci "
            "che non compaiono nelle prime 10 pagine. "
            "Restano nello storico."
        ),
        log_box,
    )

    add_debug_log(
        logs,
        "FINE FASE SCRAPING",
        log_box,
    )

    if status_box is not None:

        status_box.success(
            "Scraping completato"
        )

    return {
        "new_ads":
            new_ads,

        "known_ads":
            known_ads_count,

        "duplicate_ads":
            duplicate_ads_count,

        "new_phones":
            new_phones,

        "duplicate_phones":
            duplicate_phones,

        "pages_loaded":
            pages_loaded,

        "pages_failed":
            pages_failed,

        "errors":
            errors,
    }


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

    target_domain = clean_domain(
        target_domain
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
        timeout=SERPER_TIMEOUT,
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

    if registry_row.get(
        "hidden",
        False
    ):

        return False

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

    return (
        stored_domain
        != current_domain
    )


# ============================================================
# FASE 2 - MATCH
# ============================================================

def run_external_matches(
    target_domain,
    progress_bar=None,
    status_box=None,
    log_box=None
):

    logs = []

    add_debug_log(
        logs,
        "INIZIO FASE MATCH",
        log_box,
    )

    known_ads = (
        load_known_ads()
    )

    phone_registry = (
        load_phone_registry()
    )

    add_debug_log(
        logs,
        (
            "URL caricati per match: "
            f"{len(known_ads)}"
        ),
        log_box,
    )

    add_debug_log(
        logs,
        (
            "Telefoni registry caricati: "
            f"{len(phone_registry)}"
        ),
        log_box,
    )

    phones_by_hash = {}

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

    to_process = []

    for (
        phone_hash,
        phone
    ) in phones_by_hash.items():

        registry_row = (
            phone_registry.get(
                phone_hash
            )
        )

        if needs_external_check(
            registry_row,
            target_domain
        ):

            to_process.append(
                (
                    phone_hash,
                    phone
                )
            )

    total = len(
        to_process
    )

    add_debug_log(
        logs,
        (
            "Telefoni da processare: "
            f"{total}"
        ),
        log_box,
    )

    if total == 0:

        if status_box is not None:

            status_box.success(
                "Nessun nuovo telefono da verificare"
            )

        return {
            "queries":
                0,

            "errors":
                [],
        }

    errors = []

    query_count = 0

    for (
        index,
        (
            phone_hash,
            phone
        )
    ) in enumerate(
        to_process,
        start=1
    ):

        if status_box is not None:

            status_box.info(
                f"Match "
                f"{index}/{total} "
                f"— ID "
                f"{phone_hash[:8]}"
            )

        add_debug_log(
            logs,
            (
                f"Query "
                f"{index}/{total} "
                f"per ID "
                f"{phone_hash[:8]}"
            ),
            log_box,
        )

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

            query_count += 1

            add_debug_log(
                logs,
                (
                    f"Completato "
                    f"{phone_hash[:8]} "
                    f"- match: "
                    f"{match_count}"
                ),
                log_box,
            )

        except Exception as exc:

            error = (
                f"{phone_hash[:8]}: "
                f"{exc}"
            )

            errors.append(
                error
            )

            add_debug_log(
                logs,
                "ERRORE " + error,
                log_box,
            )

        if progress_bar is not None:

            progress_bar.progress(
                index
                / total
            )

        time.sleep(
            SERPER_DELAY
        )

    add_debug_log(
        logs,
        "FINE FASE MATCH",
        log_box,
    )

    if status_box is not None:

        status_box.success(
            "Calcolo match completato"
        )

    return {
        "queries":
            query_count,

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

    # ========================================================
    # IMPORTANTE:
    # NON FILTRIAMO PIÙ active == True
    #
    # Tutti gli annunci storici restano collegati
    # al telefono.
    # ========================================================

    if "phone_hash" not in df.columns:

        return []

    df = df[
        df[
            "phone_hash"
        ]
        .fillna("")
        .astype(str)
        .str.len()
        > 0
    ].copy()

    groups = []

    # ========================================================
    # PARTIAMO DAL REGISTRO TELEFONI
    #
    # In questo modo un telefono con note/match resta
    # visibile anche se non appare più nelle prime 10 pagine.
    # ========================================================

    for (
        phone_hash,
        reg
    ) in registry.items():

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

        group = df[
            df[
                "phone_hash"
            ] == phone_hash
        ].copy()

        cities = []

        ads = []

        if not group.empty:

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

            # Più recenti prima
            if "first_seen" in group.columns:

                group = (
                    group
                    .sort_values(
                        by="first_seen",
                        ascending=False,
                        na_position="last"
                    )
                )

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

                        "first_seen":
                            row.get(
                                "first_seen"
                            ),

                        "last_seen":
                            row.get(
                                "last_seen"
                            ),
                    }
                )

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
            }
        )

    # ========================================================
    # ORDINAMENTO
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

    .block-container {
        padding-top: 1.5rem;
        padding-bottom: 2rem;
    }

    div[data-testid="stVerticalBlockBorderWrapper"] {
        border-color: rgba(128,128,128,0.55);
    }

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
# SECRETS
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
        "Mancano i Secrets: "
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


if debug_enabled():

    st.warning(
        "🛠 DEBUG attivo"
    )


# ============================================================
# COMANDI
# ============================================================

st.subheader(
    "Aggiornamento dati"
)


button_col1, button_col2 = (
    st.columns(2)
)


with button_col1:

    scrape_button = st.button(
        "🔎 Cerca nuovi annunci",
        type="primary",
        use_container_width=True,
    )


with button_col2:

    match_button = st.button(
        "🔗 Calcola match",
        use_container_width=True,
    )


# ============================================================
# AREA LOG / PROGRESS
# ============================================================

status_box = st.empty()

progress_bar = st.progress(
    0
)

log_box = None


if debug_enabled():

    st.markdown(
        "### 🛠 Log debug"
    )

    log_box = st.empty()


# ============================================================
# ESEGUI SCRAPING
# ============================================================

if scrape_button:

    progress_bar.progress(
        0
    )

    try:

        result = (
            scrape_ads_and_phones(
                progress_bar=progress_bar,
                status_box=status_box,
                log_box=log_box,
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
            f"Duplicati intercettati: "
            f"{result['duplicate_ads']} · "
            f"Nuovi telefoni: "
            f"{result['new_phones']} · "
            f"Telefoni duplicati: "
            f"{result['duplicate_phones']}"
        )

        if result[
            "errors"
        ]:

            with st.expander(
                f"Errori / avvisi "
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
            f"Errore scraping: "
            f"{exc}"
        )


# ============================================================
# ESEGUI MATCH
# ============================================================

if match_button:

    progress_bar.progress(
        0
    )

    try:

        result = (
            run_external_matches(
                target_domain=TARGET_DOMAIN,
                progress_bar=progress_bar,
                status_box=status_box,
                log_box=log_box,
            )
        )

        st.success(
            f"Query eseguite: "
            f"{result['queries']}"
        )

        if result[
            "errors"
        ]:

            with st.expander(
                f"Errori match "
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
            f"Errore match: "
            f"{exc}"
        )


# ============================================================
# CARICA DATI
# ============================================================

try:

    ads_rows = (
        load_all_ads()
    )

    phone_registry = (
        load_phone_registry()
    )

    registry_rows = list(
        phone_registry.values()
    )

except Exception as exc:

    st.error(
        f"Errore lettura Supabase: "
        f"{exc}"
    )

    st.stop()


# ============================================================
# FILTRI
# ============================================================

st.subheader(
    "Filtri"
)


filter_col1, filter_col2 = (
    st.columns(2)
)


with filter_col1:

    hide_zero_matches = (
        st.checkbox(
            "Nascondi risultati con Match = 0",
            value=True,
        )
    )


with filter_col2:

    show_hidden = (
        st.checkbox(
            "👁 Mostra anche risultati nascosti",
            value=False,
        )
    )


groups = build_grouped_view(
    ads_rows,
    registry_rows,
    show_hidden=show_hidden,
)


if hide_zero_matches:

    groups = [
        group

        for group in groups

        if group[
            "match_count"
        ] > 0
    ]


if not groups:

    st.info(
        "Nessun risultato con i filtri attuali. "
        "Per vedere anche Match = 0, "
        "togli la relativa spunta."
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
    "Con match",
    sum(
        1

        for g in groups

        if g[
            "match_count"
        ] > 0
    )
)


m3.metric(
    "Match totali",
    sum(
        g[
            "match_count"
        ]

        for g in groups
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
    "Gli annunci storici rimangono associati "
    "al telefono anche se non compaiono più "
    "nelle prime 10 pagine del sito."
)


COLUMN_WIDTHS = [
    1.0,
    1.4,
    1.1,
    3.2,
    0.7,
    2.4,
    2.2,
    1.2,
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
# HEADER
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
# RIGHE
# ============================================================

for group in groups:

    phone_hash = (
        group[
            "phone_hash"
        ]
    )

    with st.container(
        border=True
    ):

        cols = st.columns(
            COLUMN_WIDTHS,
            gap="small"
        )

        # ----------------------------------------------------
        # ID
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # FIRST SEEN
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # CITTA
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # ANNUNCI STORICI
        # ----------------------------------------------------

        with cols[3]:

            with st.container(
                border=True
            ):

                if not group[
                    "annunci"
                ]:

                    st.caption(
                        "Nessun annuncio associato"
                    )

                else:

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

                        url = (
                            ad[
                                "url"
                            ]
                        )

                        if url:

                            st.markdown(
                                f"{index}. "
                                f"[{title}]({url})"
                            )

        # ----------------------------------------------------
        # MATCH
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # LINK ESTERNI
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # NOTE
        # ----------------------------------------------------

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

                        st.rerun()

                    except Exception as exc:

                        st.error(
                            f"Errore: "
                            f"{exc}"
                        )

        # ----------------------------------------------------
        # NASCONDI / RIPRISTINA
        # ----------------------------------------------------

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

                        set_phone_hidden(
                            phone_hash,
                            False
                        )

                        st.rerun()

                else:

                    if st.button(
                        "🙈 Nascondi",
                        key=(
                            "hide_"
                            + phone_hash
                        ),
                        use_container_width=True
                    ):

                        set_phone_hidden(
                            phone_hash,
                            True
                        )

                        st.rerun()
