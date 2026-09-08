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
    return datetime.now(timezone.utc).isoformat()


def clean_text(value):
    if not value:
        return ""

    return re.sub(
        r"\s+",
        " ",
        value
    ).strip()


def clean_domain(value):
    """
    Accetta:
    example.com
    https://example.com/
    """

    if not value:
        return ""

    value = str(value).strip().lower()

    if "://" not in value:
        value = "https://" + value

    hostname = (
        urlparse(value).hostname
        or ""
    )

    return hostname.lower().strip(".")


# ============================================================
# TELEFONO
# ============================================================

def normalize_phone(value):

    if not value:
        return ""

    value = str(value).replace(
        "tel:",
        ""
    )

    digits = re.sub(
        r"\D",
        "",
        value
    )

    # 0039xxxxxxxxxx
    if digits.startswith("0039"):
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
        normalized.encode("utf-8")
    ).hexdigest()


def extract_phone_from_detail(html):

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    candidates = []

    # --------------------------------------------------------
    # 1. href="tel:"
    # --------------------------------------------------------

    for a in soup.select(
        'a[href^="tel:"]'
    ):
        candidates.append(
            a.get("href", "")
        )

    # --------------------------------------------------------
    # 2. Attributi HTML
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
                        tag.get(attr)
                    )
                )

    # --------------------------------------------------------
    # 3. Titolo
    # --------------------------------------------------------

    if soup.title:

        candidates.append(
            soup.title.get_text(
                " ",
                strip=True
            )
        )

    # --------------------------------------------------------
    # 4. Meta
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
    # 5. Script
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
    # 6. Testo visibile
    # --------------------------------------------------------

    candidates.append(
        soup.get_text(
            " ",
            strip=True
        )
    )

    # --------------------------------------------------------
    # Cerca cellulare italiano
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


# ============================================================
# ESTRAZIONE ANNUNCI
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
        # Prova ALT immagine
        # ----------------------------------------------------

        if len(title) < 5:

            img = a.find(
                "img",
                alt=True
            )

            if img:

                title = clean_text(
                    img.get("alt")
                )

        # ----------------------------------------------------
        # Prova heading
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
                "titolo": title,
                "url": url,
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
    """
    Cerca il telefono tramite Serper.

    Restituisce:
    (
        numero_risultati,
        lista_link
    )

    Vengono mantenuti soltanto i risultati
    appartenenti a TARGET_DOMAIN.
    """

    if not phone:
        return 0, []

    if "SERPER_API_KEY" not in st.secrets:

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

    data = response.json()

    organic = (
        data.get(
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

        # ----------------------------------------------------
        # Accetta:
        # example.com
        # www.example.com
        # sottodominio.example.com
        # ----------------------------------------------------

        if (
            hostname == target_domain
            or hostname.endswith(
                "." + target_domain
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

    db = get_supabase()

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
        .execute()
    )

    return (
        response.data
        or []
    )


def load_known_ads():

    db = get_supabase()

    response = (
        db
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

    db = get_supabase()

    response = (
        db
        .table(
            PHONE_TABLE
        )
        .select("*")
        .execute()
    )

    return {

        row["phone_hash"]:
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
# SALVATAGGIO CACHE SERPER
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
    """
    Decide se dobbiamo chiamare Serper.

    NO se:
    - telefono già processato
    - dominio uguale

    SI se:
    - mai processato
    - oppure TARGET_DOMAIN è cambiato
    """

    if not registry_row:
        return True

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
# PROCESSA SOLO TELEFONI NON ANCORA CONTROLLATI
# ============================================================

def process_unchecked_phones(
    known_ads,
    phone_registry,
    target_domain
):

    query_count = 0
    errors = []

    # --------------------------------------------------------
    # phone_hash -> telefono
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Una sola query per phone_hash
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
                0.15
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

    errors = []

    now = utc_now_iso()

    # ========================================================
    # CICLO CITTÀ
    # ========================================================

    for (
        city,
        source_url
    ) in SOURCES.items():

        try:

            listing_html = get_page(
                session,
                source_url
            )

            listings = (
                extract_listing_links(
                    listing_html,
                    source_url
                )
            )

        except requests.RequestException as exc:

            errors.append(
                f"{city}: {exc}"
            )

            continue

        # ====================================================
        # CICLO ANNUNCI
        # ====================================================

        for item in listings:

            url = item[
                "url"
            ]

            current_urls.add(
                url
            )

            # =================================================
            # URL GIÀ CONOSCIUTO
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
                        f"Aggiornamento "
                        f"{url}: {exc}"
                    )

                # NON riapriamo la pagina dettaglio
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
                    f"Dettaglio "
                    f"{url}: {exc}"
                )

            # =================================================
            # TELEFONO GIÀ CONOSCIUTO?
            # =================================================

            if phone_hash:

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
                            "Telefono "
                            "esistente: "
                            f"{exc}"
                        )

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
                        }

                        new_phones += 1

                    except Exception as exc:

                        errors.append(
                            "Nuovo telefono: "
                            f"{exc}"
                        )

            # =================================================
            # SALVA ANNUNCIO
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
                    phone or "",

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
                    "Salvataggio "
                    f"annuncio {url}: "
                    f"{exc}"
                )

            time.sleep(
                0.25
            )

    # ========================================================
    # ANNUNCI NON PIÙ PRESENTI
    # ========================================================

    if current_urls:

        try:

            mark_missing_as_inactive(
                current_urls
            )

        except Exception as exc:

            errors.append(
                "Aggiornamento "
                f"inattivi: {exc}"
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

        "serper_queries":
            serper_queries,

        "errors":
            errors,
    }


# ============================================================
# COSTRUZIONE VISTA RAGGRUPPATA
# ============================================================

def build_grouped_view(
    ads_rows,
    registry_rows
):

    registry = {

        row["phone_hash"]:
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
        df["active"] == True
    ].copy()

    # --------------------------------------------------------
    # Solo record con hash telefono
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
        # Annunci associati
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
        # Risultati esterni
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
                    phone_hash[
                        :8
                    ],

                "phone_hash":
                    phone_hash,

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
            }
        )

    # ========================================================
    # ORDINAMENTO
    #
    # 1. Più match in alto
    # 2. A parità, più annunci in alto
    # ========================================================

    groups.sort(
        key=lambda x: (
            x[
                "match_count"
            ],
            x[
                "numero_annunci"
            ],
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


# ============================================================
# TARGET DOMAIN
# ============================================================

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
    f"Dominio esterno configurato: "
    f"{TARGET_DOMAIN}. "
    "Le verifiche già effettuate vengono "
    "lette da Supabase e non consumano "
    "nuove query Serper."
)


# ============================================================
# PULSANTE AGGIORNAMENTO
# ============================================================

if st.button(
    "🔄 Cerca nuovi annunci",
    type="primary",
):

    try:

        with st.spinner(
            "Controllo gli annunci e verifico "
            "soltanto i telefoni non ancora "
            "processati..."
        ):

            result = (
                scrape_and_sync(
                    TARGET_DOMAIN
                )
            )

        st.success(
            f"Nuovi annunci: "
            f"{result['new_ads']} · "
            f"Già conosciuti: "
            f"{result['known_ads']} · "
            f"Nuovi telefoni: "
            f"{result['new_phones']} · "
            f"Telefoni duplicati: "
            f"{result['duplicate_phones']} · "
            f"Query Serper eseguite: "
            f"{result['serper_queries']}"
        )

        if result[
            "errors"
        ]:

            with st.expander(
                "Dettagli errori "
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
# CARICAMENTO DATI GUI
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


groups = build_grouped_view(
    ads_rows,
    registry_rows,
)


if not groups:

    st.info(
        "Nessun gruppo disponibile. "
        "Premi “Cerca nuovi annunci”."
    )

    st.stop()


# ============================================================
# METRICHE
# ============================================================

m1, m2, m3 = (
    st.columns(3)
)


m1.metric(
    "Telefoni distinti",
    len(
        groups
    ),
)


m2.metric(
    "Con almeno un match",
    sum(
        1

        for group in groups

        if group[
            "match_count"
        ] > 0
    ),
)


m3.metric(
    "Match totali",
    sum(
        group[
            "match_count"
        ]

        for group in groups
    ),
)


# ============================================================
# RISULTATI
# ============================================================

st.subheader(
    "Risultati"
)


# ============================================================
# INTESTAZIONE TABELLA
# ============================================================

h1, h2, h3, h4, h5 = (
    st.columns(
        [
            1.1,
            1.3,
            4.0,
            1.0,
            4.0,
        ]
    )
)


h1.markdown(
    "**Telefono ID**"
)

h2.markdown(
    "**Città**"
)

h3.markdown(
    "**Annunci**"
)

h4.markdown(
    "**Match**"
)

h5.markdown(
    "**Link esterni**"
)


st.divider()


# ============================================================
# RIGHE
# ============================================================

for group in groups:

    c1, c2, c3, c4, c5 = (
        st.columns(
            [
                1.1,
                1.3,
                4.0,
                1.0,
                4.0,
            ]
        )
    )

    # --------------------------------------------------------
    # TELEFONO ID
    # --------------------------------------------------------

    with c1:

        st.code(
            group[
                "telefono_id"
            ],
            language=None,
        )

    # --------------------------------------------------------
    # CITTÀ
    # --------------------------------------------------------

    with c2:

        st.write(
            group[
                "citta"
            ]
            or "-"
        )

    # --------------------------------------------------------
    # ANNUNCI
    # --------------------------------------------------------

    with c3:

        for (
            index,
            ad
        ) in enumerate(
            group[
                "annunci"
            ],
            start=1,
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

    # --------------------------------------------------------
    # MATCH
    # --------------------------------------------------------

    with c4:

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

    # --------------------------------------------------------
    # LINK ESTERNI
    # --------------------------------------------------------

    with c5:

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
                start=1,
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

    st.divider()


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


csv_df = pd.DataFrame(
    csv_rows
)


csv_data = (
    csv_df
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
    mime="text/csv",
)
