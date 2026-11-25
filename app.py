import html
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import quote

import pandas as pd
import requests
import streamlit as st


# ---------------------------------------------------------------------
# App configuration
# ---------------------------------------------------------------------

st.set_page_config(
    page_title="Research Assistant",
    page_icon="📚",
    layout="wide",
)

HEADERS = {"User-Agent": "ResearchAssistant/1.0"}

PAPER_SOURCES = [
    "OpenAlex",
    "Crossref",
    "PubMed",
    "Europe PMC",
    "arXiv",
    "DOAJ",
    "DataCite",
]

CATEGORIES = [
    "Research Papers",
    "Corporate & Historical Archives",
    "Archaeological Records",
    "Scientific Journals & Records",
    "Historical Newspapers",
]


# ---------------------------------------------------------------------
# Secrets and password gate
# ---------------------------------------------------------------------

def get_secret(name):
    """Read a value from Streamlit Secrets or environment variables."""
    try:
        value = st.secrets.get(name, "")
    except Exception:
        value = ""

    return value or os.getenv(name, "")


app_password = get_secret("APP_PASSWORD")

if not app_password:
    st.error(
        "The app is not configured. Add APP_PASSWORD in "
        "Streamlit → Manage app → Settings → Secrets."
    )
    st.stop()

if not st.session_state.get("authenticated", False):
    st.title("📚 Research Assistant")
    st.write("Enter the app password to continue.")

    entered_password = st.text_input("App password", type="password")

    if st.button("Continue", type="primary"):
        if entered_password == app_password:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("That password is not correct.")

    st.stop()


# ---------------------------------------------------------------------
# Supabase project storage
# ---------------------------------------------------------------------

def supabase_config():
    url = get_secret("SUPABASE_URL").rstrip("/")
    key = get_secret("SUPABASE_SERVICE_ROLE_KEY")

    if not url or not key:
        raise RuntimeError(
            "Supabase is not configured. Add SUPABASE_URL and "
            "SUPABASE_SERVICE_ROLE_KEY in Streamlit Secrets."
        )

    return url, key


def supabase_request(method, path, params=None, json_body=None, prefer=None):
    url, key = supabase_config()

    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    if prefer:
        headers["Prefer"] = prefer

    response = requests.request(
        method=method,
        url=f"{url}/rest/v1/{path}",
        headers=headers,
        params=params,
        json=json_body,
        timeout=30,
    )
    response.raise_for_status()

    if not response.content:
        return None

    return response.json()


def list_projects():
    rows = supabase_request(
        "GET",
        "research_workspaces",
        params={"select": "id", "order": "id.asc"},
    )
    return [row["id"] for row in rows]


def save_project(name, payload):
    supabase_request(
        "POST",
        "research_workspaces",
        params={"on_conflict": "id"},
        json_body={
            "id": name,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "data": payload,
        },
        prefer="resolution=merge-duplicates,return=minimal",
    )


def load_project(name):
    rows = supabase_request(
        "GET",
        "research_workspaces",
        params={
            "select": "data",
            "id": f"eq.{name}",
            "limit": "1",
        },
    )
    return rows[0]["data"] if rows else None


def delete_project(name):
    supabase_request(
        "DELETE",
        "research_workspaces",
        params={"id": f"eq.{name}"},
    )


# ---------------------------------------------------------------------
# Shared data helpers
# ---------------------------------------------------------------------

def clean_text(value):
    if not value:
        return ""

    value = re.sub(r"<[^>]+>", " ", str(value))
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def make_record(
    title,
    authors="",
    year="",
    abstract="",
    url="",
    full_text_url="",
    venue="",
    source="",
    doi="",
    record_type="",
    access_notes="",
    citations="",
):
    abstract = clean_text(abstract)

    if full_text_url:
        access_status = "Document/full-text link listed; not verified"
    elif abstract:
        access_status = "Abstract, OCR snippet, or description available"
    else:
        access_status = "Catalogue/record link only"

    return {
        "title": clean_text(title) or "Untitled",
        "authors": clean_text(authors),
        "year": year or "",
        "abstract": abstract,
        "url": url or "",
        "full_text_url": full_text_url or "",
        "access_status": access_status,
        "access_notes": access_notes or "",
        "venue": clean_text(venue),
        "database": source or "",
        "doi": doi or "",
        "type": record_type or "",
        "citations": citations or "",
        "screening": "Unscreened",
        "screening_reason": "",
        "tags": "",
        "researcher_notes": "",
    }


def normalize_doi(value):
    value = (value or "").strip().lower()

    for prefix in (
        "https://doi.org/",
        "http://doi.org/",
        "doi:",
    ):
        value = value.replace(prefix, "")

    return value


def deduplicate(records):
    """Merge duplicates using DOI first and title second."""
    merged = {}

    for record in records:
        doi = normalize_doi(record.get("doi"))
        title = re.sub(
            r"\W+",
            " ",
            (record.get("title") or "").lower(),
        ).strip()

        key = doi or title
        if not key:
            continue

        if key not in merged:
            merged[key] = record.copy()
            continue

        existing = merged[key]

        for field in [
            "authors",
            "year",
            "abstract",
            "url",
            "full_text_url",
            "venue",
            "doi",
            "type",
            "citations",
            "access_notes",
        ]:
            if not existing.get(field) and record.get(field):
                existing[field] = record[field]

        if len(record.get("abstract", "")) > len(existing.get("abstract", "")):
            existing["abstract"] = record["abstract"]

        source_names = {
            item.strip()
            for item in (
                (existing.get("database", "") + "," +
                 record.get("database", "")).split(",")
            )
            if item.strip()
        }
        existing["database"] = ", ".join(sorted(source_names))

        if existing.get("full_text_url"):
            existing["access_status"] = (
                "Document/full-text link listed; not verified"
            )
        elif existing.get("abstract"):
            existing["access_status"] = (
                "Abstract, OCR snippet, or description available"
            )
        else:
            existing["access_status"] = "Catalogue/record link only"

    return list(merged.values())


def year_in_range(year, start_year, end_year):
    try:
        year_value = int(str(year)[:4])
        return int(start_year) <= year_value <= int(end_year)
    except (ValueError, TypeError):
        # Retain records if their source has no usable date.
        return True


def current_timestamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def safe_key(value):
    return re.sub(r"\W+", "_", str(value))[:60]


# ---------------------------------------------------------------------
# Research-paper search providers
# ---------------------------------------------------------------------

@st.cache_data(ttl=3600, show_spinner=False)
def search_openalex(query, start_year, end_year, limit):
    response = requests.get(
        "https://api.openalex.org/works",
        params={
            "search": query,
            "filter": (
                f"from_publication_date:{start_year}-01-01,"
                f"to_publication_date:{end_year}-12-31"
            ),
            "per-page": limit,
            "select": (
                "id,doi,title,publication_year,authorships,"
                "abstract_inverted_index,cited_by_count,primary_location,"
                "best_oa_location,type"
            ),
        },
        headers=HEADERS,
        timeout=45,
    )
    response.raise_for_status()

    records = []

    for item in response.json().get("results", []):
        inverted = item.get("abstract_inverted_index") or {}
        positioned_words = {}

        for word, positions in inverted.items():
            for position in positions:
                positioned_words[position] = word

        abstract = " ".join(
            positioned_words[position]
            for position in sorted(positioned_words)
        )

        authors = []
        for authorship in item.get("authorships", []):
            author = authorship.get("author") or {}
            if author.get("display_name"):
                authors.append(author["display_name"])

        primary_location = item.get("primary_location") or {}
        venue = (
            (primary_location.get("source") or {}).get("display_name", "")
        )

        oa_location = item.get("best_oa_location") or {}
        full_text_url = (
            oa_location.get("pdf_url")
            or oa_location.get("landing_page_url")
            or ""
        )

        doi = item.get("doi") or ""

        records.append(
            make_record(
                title=item.get("title"),
                authors=", ".join(authors),
                year=item.get("publication_year"),
                abstract=abstract,
                url=doi or item.get("id", ""),
                full_text_url=full_text_url,
                venue=venue,
                source="OpenAlex",
                doi=doi,
                record_type=item.get("type", ""),
                citations=item.get("cited_by_count", ""),
            )
        )

    return records


@st.cache_data(ttl=3600, show_spinner=False)
def search_crossref(query, start_year, end_year, limit):
    response = requests.get(
        "https://api.crossref.org/works",
        params={
            "query": query,
            "rows": limit,
            "filter": (
                f"from-pub-date:{start_year},"
                f"until-pub-date:{end_year}"
            ),
            "select": (
                "DOI,title,author,published,container-title,abstract,"
                "type,link"
            ),
        },
        headers=HEADERS,
        timeout=45,
    )
    response.raise_for_status()

    records = []

    for item in response.json().get("message", {}).get("items", []):
        titles = item.get("title") or []
        authors = []

        for author in item.get("author", []):
            name = " ".join(
                part for part in [
                    author.get("given", ""),
                    author.get("family", ""),
                ] if part
            )
            if name:
                authors.append(name)

        date_parts = (
            item.get("published", {}).get("date-parts")
            or item.get("created", {}).get("date-parts")
            or [[]]
        )
        year = date_parts[0][0] if date_parts and date_parts[0] else ""

        doi = item.get("DOI", "")
        doi_url = f"https://doi.org/{doi}" if doi else ""

        pdf_url = ""
        for link in item.get("link", []):
            if (
                "pdf" in (link.get("content-type") or "").lower()
                and link.get("URL")
            ):
                pdf_url = link["URL"]
                break

        records.append(
            make_record(
                title=titles[0] if titles else "Untitled",
                authors=", ".join(authors),
                year=year,
                abstract=item.get("abstract", ""),
                url=doi_url,
                full_text_url=pdf_url,
                venue=(item.get("container-title") or [""])[0],
                source="Crossref",
                doi=doi_url,
                record_type=item.get("type", ""),
            )
        )

    return records


@st.cache_data(ttl=3600, show_spinner=False)
def search_pubmed(query, start_year, end_year, limit):
    search_response = requests.get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
        params={
            "db": "pubmed",
            "term": (
                f"({query}) AND "
                f'("{start_year}"[Date - Publication] : '
                f'"{end_year}"[Date - Publication])'
            ),
            "retmax": limit,
            "retmode": "json",
        },
        headers=HEADERS,
        timeout=45,
    )
    search_response.raise_for_status()

    ids = (
        search_response.json()
        .get("esearchresult", {})
        .get("idlist", [])
    )
    if not ids:
        return []

    fetch_response = requests.get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
        params={
            "db": "pubmed",
            "id": ",".join(ids),
            "retmode": "xml",
        },
        headers=HEADERS,
        timeout=45,
    )
    fetch_response.raise_for_status()

    root = ET.fromstring(fetch_response.text)
    records = []

    for article in root.findall(".//PubmedArticle"):
        title_element = article.find(".//ArticleTitle")
        title = (
            "".join(title_element.itertext())
            if title_element is not None
            else ""
        )

        abstract_parts = []
        for part in article.findall(".//Abstract/AbstractText"):
            text = "".join(part.itertext()).strip()
            if text:
                label = part.attrib.get("Label", "")
                abstract_parts.append(f"{label}: {text}" if label else text)

        authors = []
        for author in article.findall(".//Author"):
            first = author.findtext("ForeName", "")
            last = author.findtext("LastName", "")
            name = " ".join(part for part in [first, last] if part)
            if name:
                authors.append(name)

        pmid = article.findtext(".//PMID", "")
        year = (
            article.findtext(".//PubDate/Year")
            or article.findtext(".//PubDate/MedlineDate", "")[:4]
        )

        pmc_id = ""
        for article_id in article.findall(".//ArticleId"):
            if article_id.attrib.get("IdType") == "pmc":
                pmc_id = article_id.text or ""
                break

        full_text_url = (
            f"https://pmc.ncbi.nlm.nih.gov/articles/{pmc_id}/"
            if pmc_id
            else ""
        )

        records.append(
            make_record(
                title=title,
                authors=", ".join(authors),
                year=year,
                abstract=" ".join(abstract_parts),
                url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                full_text_url=full_text_url,
                source="PubMed",
                record_type="Journal article",
            )
        )

    return records


@st.cache_data(ttl=3600, show_spinner=False)
def search_europe_pmc(query, start_year, end_year, limit):
    response = requests.get(
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
        params={
            "query": (
                f"({query}) AND FIRST_PDATE:[{start_year}-01-01 "
                f"TO {end_year}-12-31]"
            ),
            "format": "json",
            "resultType": "core",
            "pageSize": limit,
        },
        headers=HEADERS,
        timeout=60,
    )
    response.raise_for_status()

    results = (
        response.json()
        .get("resultList", {})
        .get("result", [])
    )
    records = []

    for item in results:
        pmcid = item.get("pmcid", "")
        source = item.get("source", "")
        paper_id = item.get("id", "")

        record_url = (
            f"https://europepmc.org/articles/{pmcid}"
            if pmcid
            else f"https://europepmc.org/article/{source}/{paper_id}"
        )

        links = (
            item.get("fullTextUrlList", {})
            .get("fullTextUrl", [])
        )
        if isinstance(links, dict):
            links = [links]

        full_text_url = next(
            (link.get("url", "") for link in links if link.get("url")),
            "",
        )

        records.append(
            make_record(
                title=item.get("title"),
                authors=item.get("authorString", ""),
                year=(item.get("firstPublicationDate") or "")[:4],
                abstract=item.get("abstractText", ""),
                url=record_url,
                full_text_url=full_text_url,
                venue=item.get("journalTitle", ""),
                source="Europe PMC",
                record_type="Journal article",
            )
        )

    return records


@st.cache_data(ttl=3600, show_spinner=False)
def search_arxiv(query, start_year, end_year, limit):
    response = requests.get(
        "https://export.arxiv.org/api/query",
        params={
            "search_query": f"all:{query}",
            "start": 0,
            "max_results": min(limit * 3, 100),
            "sortBy": "relevance",
        },
        headers=HEADERS,
        timeout=50,
    )
    response.raise_for_status()

    root = ET.fromstring(response.text)
    namespace = {"atom": "http://www.w3.org/2005/Atom"}
    records = []

    for entry in root.findall("atom:entry", namespace):
        title = " ".join(
            entry.findtext(
                "atom:title", default="", namespaces=namespace
            ).split()
        )
        abstract = " ".join(
            entry.findtext(
                "atom:summary", default="", namespaces=namespace
            ).split()
        )
        record_url = entry.findtext(
            "atom:id", default="", namespaces=namespace
        )
        published = entry.findtext(
            "atom:published", default="", namespaces=namespace
        )
        year = published[:4] if published else ""

        if year and not year_in_range(year, start_year, end_year):
            continue

        authors = [
            author.findtext(
                "atom:name", default="", namespaces=namespace
            )
            for author in entry.findall("atom:author", namespace)
        ]

        pdf_url = ""
        if record_url:
            pdf_url = record_url.replace("/abs/", "/pdf/")
            if not pdf_url.endswith(".pdf"):
                pdf_url += ".pdf"

        records.append(
            make_record(
                title=title,
                authors=", ".join(name for name in authors if name),
                year=year,
                abstract=abstract,
                url=record_url,
                full_text_url=pdf_url,
                source="arXiv",
                record_type="Preprint",
            )
        )

        if len(records) >= limit:
            break

    return records


@st.cache_data(ttl=3600, show_spinner=False)
def search_doaj(query, start_year, end_year, limit):
    endpoint = (
        "https://doaj.org/api/search/articles/"
        f"{quote(query, safe='')}"
    )
    response = requests.get(
        endpoint,
        params={"page": 1, "pageSize": limit},
        headers=HEADERS,
        timeout=45,
    )
    response.raise_for_status()

    records = []

    for result in response.json().get("results", []):
        bib = result.get("bibjson", {})
        year = bib.get("year", "")

        if year and not year_in_range(year, start_year, end_year):
            continue

        authors = [
            author.get("name", "")
            for author in bib.get("author", [])
            if author.get("name")
        ]
        links = bib.get("link", [])

        full_text_url = next(
            (
                link.get("url", "")
                for link in links
                if "fulltext" in (link.get("type") or "").lower()
                and link.get("url")
            ),
            "",
        )
        record_url = full_text_url or (
            links[0].get("url", "") if links else ""
        )

        doi_value = ""
        for identifier in bib.get("identifier", []):
            if identifier.get("type", "").lower() == "doi":
                doi_value = identifier.get("id", "")
                break

        doi_url = f"https://doi.org/{doi_value}" if doi_value else ""

        records.append(
            make_record(
                title=bib.get("title"),
                authors=", ".join(authors),
                year=year,
                abstract=bib.get("abstract", ""),
                url=record_url or doi_url,
                full_text_url=full_text_url,
                venue=bib.get("journal", {}).get("title", ""),
                source="DOAJ",
                doi=doi_url,
                record_type="Open-access journal article",
            )
        )

    return records


@st.cache_data(ttl=3600, show_spinner=False)
def search_datacite(query, start_year, end_year, limit):
    response = requests.get(
        "https://api.datacite.org/dois",
        params={
            "query": query,
            "page[size]": limit,
            "sort": "published",
        },
        headers=HEADERS,
        timeout=45,
    )
    response.raise_for_status()

    records = []

    for item in response.json().get("data", []):
        attributes = item.get("attributes", {})
        year = attributes.get("publicationYear") or ""

        if year and not year_in_range(year, start_year, end_year):
            continue

        titles = attributes.get("titles") or []
        title = titles[0].get("title", "") if titles else ""

        creators = [
            creator.get("name", "")
            for creator in attributes.get("creators", [])
            if creator.get("name")
        ]

        doi = attributes.get("doi", "")
        doi_url = f"https://doi.org/{doi}" if doi else ""

        descriptions = attributes.get("descriptions") or []
        abstract = (
            descriptions[0].get("description", "")
            if descriptions
            else ""
        )

        type_info = attributes.get("types") or {}
        record_type = (
            type_info.get("resourceTypeGeneral")
            or type_info.get("resourceType")
            or "Research output"
        )

        records.append(
            make_record(
                title=title,
                authors=", ".join(creators),
                year=year,
                abstract=abstract,
                url=doi_url,
                venue=attributes.get("publisher", ""),
                source="DataCite",
                doi=doi_url,
                record_type=record_type,
                access_notes=(
                    "Metadata is available; access to the underlying output "
                    "depends on its provider."
                ),
            )
        )

    return records


# ---------------------------------------------------------------------
# Library of Congress API
# ---------------------------------------------------------------------

@st.cache_data(ttl=3600, show_spinner=False)
def search_library_of_congress(query, limit, newspaper_only=False):
    endpoint = (
        "https://www.loc.gov/newspapers/"
        if newspaper_only
        else "https://www.loc.gov/search/"
    )

    response = requests.get(
        endpoint,
        params={"fo": "json", "q": query, "c": limit},
        headers=HEADERS,
        timeout=45,
    )
    response.raise_for_status()

    records = []

    for item in response.json().get("results", []):
        descriptions = item.get("description") or []
        if isinstance(descriptions, str):
            descriptions = [descriptions]

        resources = item.get("resources") or []
        online_formats = item.get("online_format") or []
        image_urls = item.get("image_url") or []

        listed_url = ""

        if resources and isinstance(resources[0], dict):
            listed_url = (
                resources[0].get("url")
                or resources[0].get("image_url")
                or ""
            )
        elif online_formats:
            first_format = online_formats[0]
            if isinstance(first_format, dict):
                listed_url = first_format.get("url", "")
            elif isinstance(first_format, str):
                listed_url = first_format
        elif image_urls:
            listed_url = (
                image_urls[0]
                if isinstance(image_urls, list)
                else image_urls
            )

        records.append(
            make_record(
                title=item.get("title", ""),
                year=item.get("date", ""),
                abstract=" ".join(str(value) for value in descriptions),
                url=item.get("url", ""),
                full_text_url=listed_url,
                source=(
                    "Library of Congress — Newspapers"
                    if newspaper_only
                    else "Library of Congress"
                ),
                record_type=(
                    "Newspaper catalogue record"
                    if newspaper_only
                    else "Library/archive catalogue record"
                ),
                access_notes=(
                    "A listed online resource does not guarantee complete "
                    "digitization or access."
                ),
            )
        )

    return records


# ---------------------------------------------------------------------
# Historical newspaper searches
# ---------------------------------------------------------------------

@st.cache_data(ttl=3600, show_spinner=False)
def search_internet_archive_newspapers(query, limit):
    """Search Internet Archive metadata for text items matching the query."""
    response = requests.get(
        "https://archive.org/advancedsearch.php",
        params={
            "q": f"({query}) AND mediatype:texts",
            "fl[]": ["identifier", "title", "date", "creator", "description"],
            "rows": limit,
            "page": 1,
            "output": "json",
            "sort[]": "score desc",
        },
        headers=HEADERS,
        timeout=60,
    )
    response.raise_for_status()

    docs = (
        response.json()
        .get("response", {})
        .get("docs", [])
    )
    records = []

    for item in docs:
        identifier = item.get("identifier", "")
        record_url = (
            f"https://archive.org/details/{identifier}"
            if identifier
            else "https://archive.org/"
        )

        title = item.get("title", "")
        if isinstance(title, list):
            title = "; ".join(str(value) for value in title)

        creator = item.get("creator", "")
        if isinstance(creator, list):
            creator = ", ".join(str(value) for value in creator)

        description = item.get("description", "")
        if isinstance(description, list):
            description = " ".join(str(value) for value in description)

        records.append(
            make_record(
                title=title,
                authors=creator,
                year=item.get("date", ""),
                abstract=description,
                url=record_url,
                source="Internet Archive",
                record_type="Archive catalogue record",
                access_notes=(
                    "The record may describe a digitized item, but this "
                    "search does not verify whether pages or OCR are available."
                ),
            )
        )

    return records


def pick_text(value):
    """Europeana fields can be strings, lists, or language dictionaries."""
    if isinstance(value, str):
        return value

    if isinstance(value, list):
        return "; ".join(
            pick_text(item) for item in value if item
        )

    if isinstance(value, dict):
        values = []
        for item in value.values():
            if isinstance(item, list):
                values.extend(str(x) for x in item if x)
            elif item:
                values.append(str(item))
        return "; ".join(values)

    return ""


@st.cache_data(ttl=3600, show_spinner=False)
def search_europeana_newspapers(query, limit, api_key):
    if not api_key:
        raise ValueError("EUROPEANA_API_KEY is not set in Streamlit Secrets.")

    response = requests.get(
        "https://api.europeana.eu/record/v2/search.json",
        params={
            "wskey": api_key,
            "query": query,
            "rows": limit,
            "profile": "rich",
        },
        headers=HEADERS,
        timeout=60,
    )
    response.raise_for_status()

    records = []

    for item in response.json().get("items", []):
        title = pick_text(item.get("title"))
        creators = pick_text(item.get("dcCreator"))
        date = pick_text(item.get("year")) or pick_text(item.get("timestamp"))
        description = pick_text(item.get("dcDescription"))

        record_url = (
            pick_text(item.get("edmIsShownAt"))
            or item.get("guid")
            or ""
        )

        # This is a provider-listed digital resource, not a guarantee it opens.
        digital_url = pick_text(item.get("edmIsShownBy"))

        provider = pick_text(item.get("dataProvider"))
        country = pick_text(item.get("country"))
        language = pick_text(item.get("language"))

        notes = "Europeana catalogue record."
        extra = []
        if provider:
            extra.append(f"Data provider: {provider}")
        if country:
            extra.append(f"Country: {country}")
        if language:
            extra.append(f"Language: {language}")
        if extra:
            notes += " " + " ".join(extra)

        records.append(
            make_record(
                title=title,
                authors=creators,
                year=date,
                abstract=description,
                url=record_url,
                full_text_url=digital_url,
                venue=provider,
                source="Europeana",
                record_type="Newspaper/archive catalogue record",
                access_notes=notes,
            )
        )

    return records


@st.cache_data(ttl=3600, show_spinner=False)
def search_trove_newspapers(query, limit, api_key):
    if not api_key:
        raise ValueError("TROVE_API_KEY is not set in Streamlit Secrets.")

    response = requests.get(
        "https://api.trove.nla.gov.au/v3/result",
        params={
            "key": api_key,
            "q": query,
            "zone": "newspaper",
            "encoding": "json",
            "n": limit,
        },
        headers=HEADERS,
        timeout=60,
    )
    response.raise_for_status()

    data = response.json()
    zones = data.get("response", {}).get("zone", [])
    records = []

    for zone in zones:
        articles = zone.get("records", {}).get("article", [])

        if isinstance(articles, dict):
            articles = [articles]

        for item in articles:
            title = (
                item.get("heading")
                or item.get("title")
                or item.get("newspaperTitle")
                or "Trove newspaper article"
            )

            snippet = item.get("snippet", "")
            date = item.get("date", "")
            record_url = item.get("troveUrl", "")

            newspaper = item.get("newspaperTitle", "")
            place = item.get("place", "")
            state = item.get("state", "")

            notes_parts = [
                "Trove article record. OCR snippets, when supplied, may contain recognition errors."
            ]
            if newspaper:
                notes_parts.append(f"Newspaper: {newspaper}.")
            if place:
                notes_parts.append(f"Place: {place}.")
            if state:
                notes_parts.append(f"State: {state}.")

            records.append(
                make_record(
                    title=title,
                    year=date,
                    abstract=snippet,
                    url=record_url,
                    source="Trove",
                    record_type="Newspaper article record",
                    access_notes=" ".join(notes_parts),
                )
            )

    return records


# ---------------------------------------------------------------------
# Official catalogue links
# ---------------------------------------------------------------------

def catalogue_links(query, category):
    encoded = quote((query or "").strip())

    if category == "Corporate & Historical Archives":
        return [
            (
                "National Archives of India — Abhilekh Patal",
                "India",
                "https://abhilekh-patal.in/",
                "Official portal; collection search and digitization vary.",
            ),
            (
                "Tata Central Archives",
                "India",
                "https://www.tatacentralarchives.com/",
                "Check the institution's official site for collection access.",
            ),
            (
                "Godrej Archives",
                "India",
                "https://www.godrej.com/godrej-archives",
                "Check the institution's official site for collection access.",
            ),
            (
                "National Archives Catalog (NARA)",
                "United States",
                f"https://catalog.archives.gov/search?query={encoded}",
                "Some catalogue records require an archive visit or request.",
            ),
            (
                "Library of Congress",
                "United States",
                f"https://www.loc.gov/search/?q={encoded}",
                "The app can search this source through its official API.",
            ),
            (
                "The National Archives — Discovery",
                "United Kingdom",
                (
                    "https://discovery.nationalarchives.gov.uk/results/"
                    f"r?_q={encoded}"
                ),
                "Catalogue descriptions do not necessarily link to digitized files.",
            ),
            (
                "Archives Hub",
                "United Kingdom",
                f"https://archiveshub.jisc.ac.uk/search/?terms={encoded}",
                "Search descriptions from participating UK repositories.",
            ),
        ]

    if category == "Archaeological Records":
        return [
            (
                "Archaeology Data Service",
                "United Kingdom",
                "https://archaeologydataservice.ac.uk/",
                "Search the official service; access varies by record.",
            ),
            (
                "Open Context",
                "International",
                "https://opencontext.org/",
                "Archaeological data and publication platform.",
            ),
            (
                "tDAR — Digital Archaeological Record",
                "United States / International",
                "https://www.tdar.org/",
                "Some records or files may have access restrictions.",
            ),
            (
                "Archaeological Survey of India",
                "India",
                "https://asi.nic.in/",
                "Use the official site for published resources and contacts.",
            ),
            (
                "Library of Congress",
                "United States",
                f"https://www.loc.gov/search/?q={encoded}",
                "The app can search this source through its official API.",
            ),
        ]

    if category == "Scientific Journals & Records":
        return [
            (
                "OpenAlex",
                "International",
                f"https://openalex.org/works?search={encoded}",
                "Scholarly works metadata and source links.",
            ),
            (
                "Crossref",
                "International",
                f"https://search.crossref.org/?q={encoded}",
                "Publication metadata and DOI records.",
            ),
            (
                "DataCite",
                "International",
                f"https://commons.datacite.org/?query={encoded}",
                "Research datasets and other registered research outputs.",
            ),
        ]

    if category == "Historical Newspapers":
        return [
            (
                "National Digital Library of India",
                "India",
                f"https://ndl.iitkgp.ac.in/",
                "Search the official NDLI portal; newspaper coverage varies.",
            ),
            (
                "National Archives of India — Abhilekh Patal",
                "India",
                "https://abhilekh-patal.in/",
                "Official archival catalogue; access and digitization vary.",
            ),
            (
                "National Library of India",
                "India",
                "https://www.nationallibrary.gov.in/",
                "Check the official catalogue and newspaper holdings.",
            ),
            (
                "NewspaperSG — National Library Board",
                "Singapore",
                "https://eresources.nlb.gov.sg/newspapers/",
                "Singapore newspaper archive; access may depend on location or account.",
            ),
            (
                "National Library of Malaysia",
                "Malaysia",
                "https://www.pnm.gov.my/",
                "Use the official library site/catalogue; historical newspaper coverage varies.",
            ),
            (
                "National Archives of Malaysia",
                "Malaysia",
                "https://www.arkib.gov.my/",
                "Use the official archive site for catalogue and access information.",
            ),
            (
                "National Library of Indonesia",
                "Indonesia",
                "https://www.perpusnas.go.id/",
                "Use the official library site/catalogue.",
            ),
            (
                "Khastara Digital Collections",
                "Indonesia",
                "https://khastara.perpusnas.go.id/",
                "Digital collection portal; holdings and access vary.",
            ),
            (
                "National Library of the Philippines",
                "Philippines",
                "https://web.nlp.gov.ph/",
                "Use the official library site for catalogue and access information.",
            ),
            (
                "National Library of Thailand",
                "Thailand",
                "https://www.nlt.go.th/",
                "Use the official library site; search and digitization options may vary.",
            ),
            (
                "National Library of Vietnam",
                "Vietnam",
                "https://nlv.gov.vn/",
                "Use the official library site/catalogue.",
            ),
            (
                "National Archives of Brunei Darussalam",
                "Brunei",
                "https://www.arkib.gov.bn/",
                "Use the official archive site for holdings and access information.",
            ),
            (
                "National Library of Cambodia",
                "Cambodia",
                "https://www.nlc.gov.kh/",
                "Use the official library site; online catalogue coverage may vary.",
            ),
            (
                "National Library of Laos",
                "Laos",
                "https://www.lao-nlp.org/",
                "Check the official site; online historical newspaper access may be limited.",
            ),
            (
                "National Library of Myanmar",
                "Myanmar",
                "https://www.nlm.gov.mm/",
                "Check the official site; catalogue and digitized holdings may vary.",
            ),
            (
                "Trove Newspapers",
                "Australia",
                "https://trove.nla.gov.au/newspaper/",
                "Trove API search is available after adding TROVE_API_KEY.",
            ),
            (
                "Europeana",
                "Europe / International",
                f"https://www.europeana.eu/en/search?query={encoded}",
                "Europeana results depend on participating providers and their access settings.",
            ),
            (
                "Internet Archive",
                "International",
                f"https://archive.org/search?query={encoded}",
                "Search results may be catalogue records; item access varies.",
            ),
            (
                "Library of Congress — Newspapers",
                "United States",
                f"https://www.loc.gov/newspapers/?q={encoded}",
                "The app searches this official API when you run a newspaper search.",
            ),
        ]

    return []


def show_catalogue_links(query, category):
    st.markdown("#### Official catalogue links")
    st.caption(
        "These are links to official institution sites. The app does not "
        "scrape them. Holdings, digitization, OCR, and public access vary."
    )

    for institution, country, url, note in catalogue_links(query, category):
        st.markdown(
            f"**{institution}** ({country})  \n"
            f"[Open official catalogue/site]({url})  \n"
            f"{note}"
        )


# ---------------------------------------------------------------------
# Search logs
# ---------------------------------------------------------------------

def add_search_log(category, query, sources, start_year="", end_year="",
                   limit="", outcomes=None):
    logs = st.session_state.setdefault("search_logs", [])
    logs.append({
        "searched_at": current_timestamp(),
        "category": category,
        "query": query,
        "sources": ", ".join(sources) if isinstance(sources, list) else sources,
        "from_year": start_year,
        "to_year": end_year,
        "limit": limit,
        "outcomes": json.dumps(outcomes or {}, ensure_ascii=False),
    })


def show_search_logs():
    logs = st.session_state.get("search_logs", [])

    with st.expander("Reproducible search log"):
        if not logs:
            st.caption("No searches logged in this project yet.")
        else:
            st.dataframe(
                pd.DataFrame(logs),
                use_container_width=True,
                hide_index=True,
            )


# ---------------------------------------------------------------------
# Citation checks, exports, and AI outline
# ---------------------------------------------------------------------

def citation_issues(record):
    issues = []

    if not record.get("title"):
        issues.append("Missing title")
    if not record.get("authors"):
        issues.append("Authors not supplied")
    if not record.get("year"):
        issues.append("Year/date not supplied")
    if not record.get("url"):
        issues.append("No record URL")
    if not record.get("doi"):
        issues.append("No DOI supplied")

    return issues


def citation_quality_summary(records):
    if not records:
        return

    count = sum(bool(citation_issues(record)) for record in records)
    st.caption(
        f"Basic metadata check: {count} of {len(records)} records have "
        "one or more missing fields. This does not verify citation accuracy."
    )


def make_bibtex(records):
    entries = []

    for index, record in enumerate(records, start=1):
        author_text = record.get("authors") or ""
        first_author = author_text.split(",")[0] if author_text else "source"
        last_name = re.sub(
            r"[^A-Za-z0-9]",
            "",
            first_author.split()[-1],
        )
        year = str(record.get("year") or "nd")
        citation_key = f"{last_name or 'source'}{year}{index}"

        fields = {
            "title": record.get("title", ""),
            "author": author_text,
            "year": year if year != "nd" else "",
            "journal": record.get("venue", ""),
            "doi": normalize_doi(record.get("doi", "")),
            "url": record.get("url", ""),
        }

        lines = [f"@misc{{{citation_key},"]
        for field, value in fields.items():
            if value:
                safe_value = str(value).replace("{", "").replace("}", "")
                lines.append(f"  {field} = {{{safe_value}}},")
        lines.append("}")
        entries.append("\n".join(lines))

    return "\n\n".join(entries)


def make_ris(records):
    entries = []

    for record in records:
        lines = ["TY  - GEN"]

        if record.get("title"):
            lines.append(f"TI  - {record['title']}")

        for author in (record.get("authors") or "").split(","):
            if author.strip():
                lines.append(f"AU  - {author.strip()}")

        if record.get("year"):
            lines.append(f"PY  - {record['year']}")

        if record.get("venue"):
            lines.append(f"JO  - {record['venue']}")

        doi = normalize_doi(record.get("doi", ""))
        if doi:
            lines.append(f"DO  - {doi}")

        if record.get("url"):
            lines.append(f"UR  - {record['url']}")

        if record.get("abstract"):
            lines.append(f"AB  - {record['abstract']}")

        lines.extend(["ER  - ", ""])
        entries.append("\n".join(lines))

    return "\n".join(entries)


def make_markdown(question, records, outline=""):
    lines = [f"# {question or 'Research project'}", ""]

    if outline:
        lines.extend([outline, ""])

    lines.append("## Sources")

    for index, record in enumerate(records, start=1):
        title = record.get("title") or "Untitled"
        url = record.get("url") or ""
        lines.append(
            f"{index}. [{title}]({url})" if url else f"{index}. {title}"
        )
        lines.append(f"   - Authors: {record.get('authors') or 'Not listed'}")
        lines.append(f"   - Date/year: {record.get('year') or 'Not listed'}")
        lines.append(f"   - Source: {record.get('database') or 'Not listed'}")
        lines.append(
            f"   - Access: {record.get('access_status') or 'Not listed'}"
        )

        if record.get("full_text_url"):
            lines.append(
                f"   - Listed document link: {record['full_text_url']}"
            )
        if record.get("abstract"):
            lines.append(f"   - Abstract/description: {record['abstract']}")
        lines.append("")

    return "\n".join(lines)


def linkify_citations(text, records):
    def replace(match):
        number = int(match.group(1))
        position = number - 1

        if position < 0 or position >= len(records):
            return match.group(0)

        url = records[position].get("url")
        return f"[S{number}]({url})" if url else match.group(0)

    return re.sub(r"\[S(\d+)\]", replace, text)


def make_reference_list(records):
    lines = ["#### References"]

    for index, record in enumerate(records, start=1):
        authors = record.get("authors") or "Authors not listed"
        year = record.get("year") or "Date not listed"
        title = record.get("title") or "Untitled"
        venue = record.get("venue") or ""
        url = record.get("url") or ""

        reference = f"[S{index}] {authors} ({year}). *{title}*."
        if venue:
            reference += f" {venue}."
        if url:
            reference += f" [Open record]({url})"
        lines.append(reference)

    return "\n".join(lines)


def generate_outline(question, records):
    openai_key = get_secret("OPENAI_API_KEY")
    gemini_key = get_secret("GEMINI_API_KEY")

    if not openai_key and not gemini_key:
        raise RuntimeError(
            "Add OPENAI_API_KEY or GEMINI_API_KEY in Streamlit Secrets "
            "to generate an AI outline."
        )

    source_blocks = []

    for index, record in enumerate(records, start=1):
        source_blocks.append(
            f"[S{index}] {record.get('title', 'Untitled')}\n"
            f"Authors: {record.get('authors') or 'Not supplied'}\n"
            f"Year: {record.get('year') or 'Not supplied'}\n"
            f"Abstract/description: "
            f"{(record.get('abstract') or 'Not supplied')[:1500]}\n"
            f"Record URL: {record.get('url') or 'Not supplied'}\n"
            f"Access label: {record.get('access_status') or 'Not supplied'}"
        )

    prompt = f"""
Research question: {question}

Write a cautious literature-review outline using only the source
information below.

Rules:
- Cite evidence-based points with source IDs such as [S1].
- Do not invent findings, methods, authors, dates, or citations.
- Say when the supplied abstract or description is insufficient.
- Do not claim to have read full text based only on a link.
- This is a draft for a researcher to check against original sources.

Sources:
{chr(10).join(source_blocks)}
"""

    # Prefer OpenAI if its key is configured; otherwise use Gemini.
    if openai_key:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {openai_key}"},
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are a cautious research assistant. "
                            "Never invent evidence or citations."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
            },
            timeout=90,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    model = get_secret("GEMINI_MODEL") or "gemini-2.5-flash"
    response = requests.post(
        "https://generativelanguage.googleapis.com/v1beta/"
        f"models/{model}:generateContent",
        params={"key": gemini_key},
        json={
            "systemInstruction": {
                "parts": [{
                    "text": (
                        "You are a cautious research assistant. "
                        "Never invent evidence or citations."
                    )
                }]
            },
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2},
        },
        timeout=90,
    )
    response.raise_for_status()

    data = response.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


# ---------------------------------------------------------------------
# Filtering, screening, notes, and record display
# ---------------------------------------------------------------------

def filter_records(records, key_prefix):
    sources = sorted({
        source.strip()
        for record in records
        for source in (record.get("database") or "").split(",")
        if source.strip()
    })

    chosen_sources = st.multiselect(
        "Filter by source",
        options=sources,
        default=sources,
        key=f"{key_prefix}_filter_sources",
    )

    years = []
    for record in records:
        try:
            years.append(int(str(record.get("year"))[:4]))
        except (ValueError, TypeError):
            pass

    year_range = None
    if years and min(years) < max(years):
        year_range = st.slider(
            "Filter by year",
            min_value=min(years),
            max_value=max(years),
            value=(min(years), max(years)),
            key=f"{key_prefix}_filter_years",
        )

    sort_choice = st.selectbox(
        "Sort results",
        [
            "Relevance/order found",
            "Newest first",
            "Oldest first",
            "Most cited",
        ],
        key=f"{key_prefix}_sort",
    )

    filtered = []

    for record in records:
        record_sources = {
            source.strip()
            for source in (record.get("database") or "").split(",")
            if source.strip()
        }

        if chosen_sources and not record_sources.intersection(chosen_sources):
            continue

        if year_range:
            try:
                year = int(str(record.get("year"))[:4])
                if not year_range[0] <= year <= year_range[1]:
                    continue
            except (ValueError, TypeError):
                pass

        filtered.append(record)

    if sort_choice == "Newest first":
        filtered.sort(
            key=lambda item: int(str(item.get("year") or "0")[:4]),
            reverse=True,
        )
    elif sort_choice == "Oldest first":
        filtered.sort(
            key=lambda item: int(str(item.get("year") or "9999")[:4])
        )
    elif sort_choice == "Most cited":
        filtered.sort(
            key=lambda item: int(item.get("citations") or 0),
            reverse=True,
        )

    return filtered


def render_records(category, records, question, allow_ai=False):
    if not records:
        st.info("No results yet. Run a search above.")
        return

    visible = filter_records(records, safe_key(category))
    st.write(f"Showing **{len(visible)}** records.")
    citation_quality_summary(visible)

    rows = []
    for record in visible:
        issues = citation_issues(record)
        rows.append({
            "Title": record.get("title", ""),
            "Authors": record.get("authors", ""),
            "Date/year": record.get("year", ""),
            "Venue/institution": record.get("venue", ""),
            "Source": record.get("database", ""),
            "Access status": record.get("access_status", ""),
            "Citation check": (
                "; ".join(issues) if issues else "Basic fields present"
            ),
            "Record link": record.get("url", ""),
            "Listed document link": record.get("full_text_url", ""),
        })

    st.dataframe(
        pd.DataFrame(rows),
        use_container_width=True,
        hide_index=True,
        column_config={
            "Record link": st.column_config.LinkColumn("Record link"),
            "Listed document link": st.column_config.LinkColumn(
                "Listed document link"
            ),
        },
    )

    st.caption(
        "A listed document link is not verified. It may require an account, "
        "payment, permission, or an archive visit."
    )

    col1, col2, col3, col4 = st.columns(4)

    with col1:
        st.download_button(
            "Download CSV",
            data=pd.DataFrame(visible).to_csv(index=False).encode("utf-8"),
            file_name="research_results.csv",
            mime="text/csv",
            key=f"{safe_key(category)}_csv",
        )

    with col2:
        st.download_button(
            "Download BibTeX",
            data=make_bibtex(visible),
            file_name="research_results.bib",
            mime="application/x-bibtex",
            key=f"{safe_key(category)}_bib",
        )

    with col3:
        st.download_button(
            "Download RIS",
            data=make_ris(visible),
            file_name="research_results.ris",
            mime="application/x-research-info-systems",
            key=f"{safe_key(category)}_ris",
        )

    outline = st.session_state.get(f"{category}_outline", "")

    with col4:
        st.download_button(
            "Download Markdown",
            data=make_markdown(question, visible, outline),
            file_name="research_results.md",
            mime="text/markdown",
            key=f"{safe_key(category)}_markdown",
        )

    st.markdown("#### Screening and researcher notes")
    st.caption(
        "Make changes inside each record, then save the project to preserve them."
    )

    for index, record in enumerate(visible):
        title = record.get("title") or "Untitled"
        record_identity = (
            record.get("doi")
            or record.get("url")
            or f"{title}_{index}"
        )
        form_key = f"{safe_key(category)}_record_{safe_key(record_identity)}"

        with st.expander(f"{index + 1}. {title}"):
            st.write(
                f"**Source:** {record.get('database') or 'Not listed'}  \n"
                f"**Date/year:** {record.get('year') or 'Not listed'}  \n"
                f"**Access:** {record.get('access_status') or 'Not listed'}"
            )

            if record.get("url"):
                st.markdown(f"[Open record/catalogue]({record['url']})")
            if record.get("full_text_url"):
                st.markdown(
                    f"[Open listed document link]({record['full_text_url']})"
                )
            if record.get("access_notes"):
                st.caption(record["access_notes"])

            st.write(
                record.get("abstract")
                or "No abstract, OCR snippet, or description was supplied."
            )

            with st.form(form_key):
                screening_options = [
                    "Unscreened",
                    "Include",
                    "Exclude",
                    "Maybe",
                ]
                current_screening = record.get("screening", "Unscreened")
                screening_index = (
                    screening_options.index(current_screening)
                    if current_screening in screening_options
                    else 0
                )

                screening = st.selectbox(
                    "Screening decision",
                    screening_options,
                    index=screening_index,
                )
                reason = st.text_input(
                    "Reason for decision",
                    value=record.get("screening_reason", ""),
                )
                tags = st.text_input(
                    "Tags",
                    value=record.get("tags", ""),
                )
                notes = st.text_area(
                    "Researcher notes",
                    value=record.get("researcher_notes", ""),
                )

                if st.form_submit_button("Save screening and notes"):
                    record["screening"] = screening
                    record["screening_reason"] = reason
                    record["tags"] = tags
                    record["researcher_notes"] = notes
                    st.success("Notes updated. Save the project to keep them.")

    if allow_ai and visible:
        if st.button(
            "Generate evidence-linked outline",
            key=f"{safe_key(category)}_generate_ai",
        ):
            try:
                with st.spinner("Generating an abstract/record-based outline..."):
                    outline = generate_outline(question, visible)

                st.session_state[f"{category}_outline"] = outline
                st.session_state[f"{category}_outline_records"] = [
                    record.copy() for record in visible
                ]
                st.rerun()

            except requests.RequestException as exc:
                st.error(f"AI service request failed: {exc}")
            except Exception as exc:
                st.error(str(exc))

        outline = st.session_state.get(f"{category}_outline", "")
        outline_records = st.session_state.get(
            f"{category}_outline_records", []
        )

        if outline:
            st.subheader("AI draft outline")
            st.markdown(linkify_citations(outline, outline_records))
            st.markdown(make_reference_list(outline_records))
            st.warning(
                "The outline uses supplied abstracts or descriptions. "
                "Verify every claim against the original sources."
            )


# ---------------------------------------------------------------------
# Catalogue links
# ---------------------------------------------------------------------

def show_catalogue_links(query, category):
    st.markdown("#### Official catalogue links")
    st.caption(
        "These are official-site links, not results automatically searched "
        "by this app. Holdings and access vary."
    )

    for institution, country, url, note in catalogue_links(query, category):
        st.markdown(
            f"**{institution}** ({country})  \n"
            f"[Open official catalogue/site]({url})  \n"
            f"{note}"
        )


# ---------------------------------------------------------------------
# Sidebar: load/delete projects
# ---------------------------------------------------------------------

with st.sidebar:
    st.header("Saved projects")

    try:
        project_names = list_projects()

        if project_names:
            selected_project = st.selectbox(
                "Choose a project",
                project_names,
                key="project_to_load",
            )

            if st.button("Load project"):
                payload = load_project(selected_project)
                if payload:
                    st.session_state["loaded_project_payload"] = payload
                    st.session_state["loaded_project_name"] = selected_project
                    st.rerun()

            if st.button("Delete project"):
                delete_project(selected_project)
                st.success("Project deleted.")
                st.rerun()
        else:
            st.caption("No saved projects yet.")

    except Exception as exc:
        st.error(f"Could not connect to Supabase: {exc}")

    st.divider()

    if st.button("Log out"):
        st.session_state["authenticated"] = False
        st.rerun()


# Restore project data after loading.
loaded_payload = st.session_state.pop("loaded_project_payload", None)

if loaded_payload:
    loaded_category = loaded_payload.get("category", "Research Papers")
    st.session_state[f"{loaded_category}_records"] = loaded_payload.get(
        "records", []
    )
    st.session_state[f"{loaded_category}_question"] = loaded_payload.get(
        "question", ""
    )
    st.session_state[f"{loaded_category}_outline"] = loaded_payload.get(
        "outline", ""
    )
    st.session_state[f"{loaded_category}_outline_records"] = loaded_payload.get(
        "outline_records", []
    )
    st.session_state["search_logs"] = loaded_payload.get("search_logs", [])


# ---------------------------------------------------------------------
# Main interface
# ---------------------------------------------------------------------

st.title("📚 Research Assistant")
st.write(
    "Search research papers, historical archives, archaeological records, "
    "scientific outputs, and historical newspapers."
)

st.info(
    "A catalogue record, abstract/OCR snippet, and listed digitized document "
    "are different things. The app cannot guarantee that a link is complete, "
    "readable, or freely accessible."
)

paper_tab, corporate_tab, archaeology_tab, science_tab, newspaper_tab = (
    st.tabs(CATEGORIES)
)


# ---------------------------------------------------------------------
# Research Papers
# ---------------------------------------------------------------------

with paper_tab:
    st.subheader("Research Papers")

    with st.form("papers_search_form"):
        query = st.text_input(
            "Research question or topic",
            key="papers_query_input",
            placeholder="Example: effects of urban green space on health",
        )

        c1, c2, c3 = st.columns(3)
        with c1:
            start_year = st.number_input(
                "From year", 1800, 2100, 2015, key="papers_start_year"
            )
        with c2:
            end_year = st.number_input(
                "To year", 1800, 2100, 2025, key="papers_end_year"
            )
        with c3:
            limit = st.slider(
                "Results per source", 5, 50, 20, key="papers_limit"
            )

        sources = st.multiselect(
            "Sources",
            PAPER_SOURCES,
            default=PAPER_SOURCES,
            key="papers_sources",
        )

        submitted = st.form_submit_button(
            "Search research papers",
            type="primary",
        )

    if submitted:
        if not query.strip():
            st.error("Enter a research topic.")
        elif start_year > end_year:
            st.error("From year must not be later than To year.")
        else:
            searchers = {
                "OpenAlex": search_openalex,
                "Crossref": search_crossref,
                "PubMed": search_pubmed,
                "Europe PMC": search_europe_pmc,
                "arXiv": search_arxiv,
                "DOAJ": search_doaj,
                "DataCite": search_datacite,
            }

            found = []
            outcomes = {}

            for source in sources:
                try:
                    with st.spinner(f"Searching {source}..."):
                        results = searchers[source](
                            query, int(start_year), int(end_year), int(limit)
                        )
                    found.extend(results)
                    outcomes[source] = f"Success: {len(results)} results"
                except Exception as exc:
                    outcomes[source] = f"Failed: {str(exc)[:200]}"
                    st.warning(f"{source} search failed: {exc}")

            st.session_state["Research Papers_records"] = deduplicate(found)
            st.session_state["Research Papers_question"] = query
            st.session_state["Research Papers_outline"] = ""
            st.session_state["Research Papers_outline_records"] = []

            add_search_log(
                "Research Papers",
                query,
                sources,
                int(start_year),
                int(end_year),
                int(limit),
                outcomes,
            )

    records = st.session_state.get("Research Papers_records", [])
    query = st.session_state.get(
        "Research Papers_question",
        st.session_state.get("papers_query_input", ""),
    )

    render_records("Research Papers", records, query, allow_ai=True)


# ---------------------------------------------------------------------
# Corporate & Historical Archives
# ---------------------------------------------------------------------

with corporate_tab:
    st.subheader("Corporate & Historical Archives")
    st.write(
        "The app directly searches the Library of Congress API. Other "
        "institutions are linked through their official catalogues."
    )

    with st.form("corporate_search_form"):
        query = st.text_input(
            "Company, organization, person, or topic",
            key="corporate_query_input",
            placeholder="Example: Tata Iron and Steel Company",
        )
        limit = st.slider(
            "Library of Congress results",
            5, 50, 20,
            key="corporate_loc_limit",
        )
        submitted = st.form_submit_button(
            "Search archives",
            type="primary",
        )

    if submitted:
        if not query.strip():
            st.error("Enter a company, organization, person, or topic.")
        else:
            try:
                with st.spinner("Searching Library of Congress..."):
                    results = search_library_of_congress(query, int(limit))

                st.session_state[
                    "Corporate & Historical Archives_records"
                ] = results
                st.session_state[
                    "Corporate & Historical Archives_question"
                ] = query

                add_search_log(
                    "Corporate & Historical Archives",
                    query,
                    ["Library of Congress API"],
                    outcomes={
                        "Library of Congress": f"Success: {len(results)} results"
                    },
                )
            except Exception as exc:
                st.warning(f"Library of Congress search failed: {exc}")

    records = st.session_state.get(
        "Corporate & Historical Archives_records", []
    )
    query = st.session_state.get(
        "Corporate & Historical Archives_question",
        st.session_state.get("corporate_query_input", ""),
    )

    if records:
        render_records(
            "Corporate & Historical Archives",
            records,
            query,
            allow_ai=False,
        )

    if query.strip():
        show_catalogue_links(query, "Corporate & Historical Archives")


# ---------------------------------------------------------------------
# Archaeological Records
# ---------------------------------------------------------------------

with archaeology_tab:
    st.subheader("Archaeological Records")
    st.write(
        "This section links to official archaeology catalogues. It does not "
        "scrape those sites."
    )

    with st.form("archaeology_search_form"):
        query = st.text_input(
            "Site, region, period, project, or topic",
            key="archaeology_query_input",
            placeholder="Example: Indus Valley excavation",
        )
        submitted = st.form_submit_button(
            "Show archaeology catalogues",
            type="primary",
        )

    if submitted:
        if not query.strip():
            st.error("Enter a site, region, period, project, or topic.")
        else:
            st.session_state["Archaeological Records_question"] = query
            add_search_log(
                "Archaeological Records",
                query,
                ["Official catalogue links"],
                outcomes={
                    "Catalogue links": "Shown; not directly searched by app"
                },
            )

    query = st.session_state.get(
        "Archaeological Records_question",
        st.session_state.get("archaeology_query_input", ""),
    )

    if query.strip():
        show_catalogue_links(query, "Archaeological Records")


# ---------------------------------------------------------------------
# Scientific Journals & Records
# ---------------------------------------------------------------------

with science_tab:
    st.subheader("Scientific Journals & Records")

    with st.form("science_search_form"):
        query = st.text_input(
            "Scientific topic or research-output query",
            key="science_query_input",
            placeholder="Example: climate change crop yield dataset",
        )

        c1, c2, c3 = st.columns(3)
        with c1:
            start_year = st.number_input(
                "From year", 1800, 2100, 2015, key="science_start_year"
            )
        with c2:
            end_year = st.number_input(
                "To year", 1800, 2100, 2025, key="science_end_year"
            )
        with c3:
            limit = st.slider(
                "Results per source", 5, 50, 20, key="science_limit"
            )

        sources = st.multiselect(
            "Scientific sources",
            PAPER_SOURCES,
            default=[
                "OpenAlex",
                "Crossref",
                "PubMed",
                "Europe PMC",
                "DataCite",
            ],
            key="science_sources",
        )

        submitted = st.form_submit_button(
            "Search scientific records",
            type="primary",
        )

    if submitted:
        if not query.strip():
            st.error("Enter a scientific topic.")
        elif start_year > end_year:
            st.error("From year must not be later than To year.")
        else:
            searchers = {
                "OpenAlex": search_openalex,
                "Crossref": search_crossref,
                "PubMed": search_pubmed,
                "Europe PMC": search_europe_pmc,
                "arXiv": search_arxiv,
                "DOAJ": search_doaj,
                "DataCite": search_datacite,
            }

            found = []
            outcomes = {}

            for source in sources:
                try:
                    with st.spinner(f"Searching {source}..."):
                        results = searchers[source](
                            query, int(start_year), int(end_year), int(limit)
                        )
                    found.extend(results)
                    outcomes[source] = f"Success: {len(results)} results"
                except Exception as exc:
                    outcomes[source] = f"Failed: {str(exc)[:200]}"
                    st.warning(f"{source} search failed: {exc}")

            st.session_state[
                "Scientific Journals & Records_records"
            ] = deduplicate(found)
            st.session_state[
                "Scientific Journals & Records_question"
            ] = query
            st.session_state[
                "Scientific Journals & Records_outline"
            ] = ""
            st.session_state[
                "Scientific Journals & Records_outline_records"
            ] = []

            add_search_log(
                "Scientific Journals & Records",
                query,
                sources,
                int(start_year),
                int(end_year),
                int(limit),
                outcomes,
            )

    records = st.session_state.get(
        "Scientific Journals & Records_records", []
    )
    query = st.session_state.get(
        "Scientific Journals & Records_question",
        st.session_state.get("science_query_input", ""),
    )

    render_records(
        "Scientific Journals & Records",
        records,
        query,
        allow_ai=True,
    )

    if query.strip():
        show_catalogue_links(query, "Scientific Journals & Records")


# ---------------------------------------------------------------------
# Historical Newspapers
# ---------------------------------------------------------------------

with newspaper_tab:
    st.subheader("Historical Newspapers")
    st.write(
        "Search Internet Archive and Library of Congress directly. "
        "Europeana search uses your API key. Trove search becomes available "
        "when you add its API key."
    )

    with st.form("newspaper_search_form"):
        query = st.text_input(
            "Newspaper title, place, date, language, or topic",
            key="newspaper_query_input",
            placeholder="Example: 1910 Bombay newspaper",
        )

        limit = st.slider(
            "Results per direct-search source",
            5,
            50,
            20,
            key="newspaper_limit",
        )

        selected_sources = st.multiselect(
            "Newspaper sources to search",
            [
                "Internet Archive",
                "Library of Congress",
                "Europeana",
                "Trove",
            ],
            default=[
                "Internet Archive",
                "Library of Congress",
                "Europeana",
            ],
            key="newspaper_sources",
        )

        submitted = st.form_submit_button(
            "Search historical newspapers",
            type="primary",
        )

    if submitted:
        if not query.strip():
            st.error("Enter a newspaper title, place, date, language, or topic.")
        else:
            found = []
            outcomes = {}

            for source in selected_sources:
                try:
                    with st.spinner(f"Searching {source}..."):
                        if source == "Internet Archive":
                            results = search_internet_archive_newspapers(
                                query, int(limit)
                            )
                        elif source == "Library of Congress":
                            results = search_library_of_congress(
                                query,
                                int(limit),
                                newspaper_only=True,
                            )
                        elif source == "Europeana":
                            api_key = get_secret("EUROPEANA_API_KEY")
                            results = search_europeana_newspapers(
                                query,
                                int(limit),
                                api_key,
                            )
                        elif source == "Trove":
                            api_key = get_secret("TROVE_API_KEY")
                            results = search_trove_newspapers(
                                query,
                                int(limit),
                                api_key,
                            )
                        else:
                            results = []

                    found.extend(results)
                    outcomes[source] = f"Success: {len(results)} results"

                except Exception as exc:
                    outcomes[source] = f"Failed: {str(exc)[:200]}"
                    st.warning(f"{source} search failed: {exc}")

            st.session_state["Historical Newspapers_records"] = deduplicate(found)
            st.session_state["Historical Newspapers_question"] = query

            add_search_log(
                "Historical Newspapers",
                query,
                selected_sources,
                limit=int(limit),
                outcomes=outcomes,
            )

    newspaper_records = st.session_state.get(
        "Historical Newspapers_records", []
    )
    newspaper_query = st.session_state.get(
        "Historical Newspapers_question",
        st.session_state.get("newspaper_query_input", ""),
    )

    if newspaper_records:
        render_records(
            "Historical Newspapers",
            newspaper_records,
            newspaper_query,
            allow_ai=False,
        )
    else:
        st.caption(
            "Search results will appear here. If Europeana or Trove is "
            "selected without a valid API key, that source will show a warning."
        )

    if newspaper_query.strip():
        show_catalogue_links(
            newspaper_query,
            "Historical Newspapers",
        )


# ---------------------------------------------------------------------
# Project logs and saving
# ---------------------------------------------------------------------

st.divider()

with st.expander("Reproducible search log"):
    logs = st.session_state.get("search_logs", [])
    if logs:
        st.dataframe(
            pd.DataFrame(logs),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.caption("No searches logged in this project yet.")

st.subheader("Save project to Supabase")

category_to_save = st.selectbox(
    "Section to save",
    CATEGORIES,
    key="category_to_save",
)

project_name = st.text_input(
    "Project name",
    value=st.session_state.get("loaded_project_name", ""),
    key="save_project_name",
)

if st.button("Save project"):
    records = st.session_state.get(f"{category_to_save}_records", [])
    question = st.session_state.get(f"{category_to_save}_question", "")
    outline = st.session_state.get(f"{category_to_save}_outline", "")
    outline_records = st.session_state.get(
        f"{category_to_save}_outline_records", []
    )

    if not project_name.strip():
        st.error("Enter a project name.")
    elif not question and not records:
        st.error("Search for something before saving.")
    else:
        payload = {
            "category": category_to_save,
            "question": question,
            "records": records,
            "outline": outline,
            "outline_records": outline_records,
            "search_logs": st.session_state.get("search_logs", []),
            "saved_at": current_timestamp(),
        }

        try:
            save_project(project_name.strip(), payload)
            st.session_state["loaded_project_name"] = project_name.strip()
            st.success(f"Project saved to Supabase: {project_name.strip()}")
        except Exception as exc:
            st.error(f"Could not save project to Supabase: {exc}")
