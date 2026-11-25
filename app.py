import html
import json
import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import quote

import pandas as pd
import requests
import streamlit as st


# ---------------------------------------------------------------------------
# App configuration
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Research Assistant",
    page_icon="📚",
    layout="wide",
)

HEADERS = {"User-Agent": "ResearchAssistant/1.0"}
DB_PATH = "research_projects.db"

PAPER_SOURCES = [
    "OpenAlex",
    "Crossref",
    "PubMed",
    "Europe PMC",
    "arXiv",
    "DOAJ",
    "DataCite",
]


# ---------------------------------------------------------------------------
# Secrets and saved projects
# ---------------------------------------------------------------------------

def get_secret(name):
    """Read a key from Streamlit secrets or environment variables."""
    try:
        value = st.secrets.get(name, "")
    except Exception:
        value = ""

    return value or os.getenv(name, "")


def init_project_db():
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS projects (
                name TEXT PRIMARY KEY,
                updated_at TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        connection.commit()


def list_projects():
    init_project_db()

    with sqlite3.connect(DB_PATH) as connection:
        rows = connection.execute(
            "SELECT name FROM projects ORDER BY name"
        ).fetchall()

    return [row[0] for row in rows]


def save_project(name, payload):
    init_project_db()

    updated_at = datetime.now(timezone.utc).isoformat()

    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            """
            INSERT INTO projects (name, updated_at, payload)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                updated_at = excluded.updated_at,
                payload = excluded.payload
            """,
            (name, updated_at, json.dumps(payload)),
        )
        connection.commit()


def load_project(name):
    init_project_db()

    with sqlite3.connect(DB_PATH) as connection:
        row = connection.execute(
            "SELECT payload FROM projects WHERE name = ?",
            (name,),
        ).fetchone()

    return json.loads(row[0]) if row else None


def delete_project(name):
    init_project_db()

    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            "DELETE FROM projects WHERE name = ?",
            (name,),
        )
        connection.commit()


# ---------------------------------------------------------------------------
# Shared record helpers
# ---------------------------------------------------------------------------

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
        access_status = "Full-text link listed (not verified)"
    elif abstract:
        access_status = "Abstract or description available"
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
    }


def normalize_doi(value):
    value = (value or "").strip().lower()
    value = value.replace("https://doi.org/", "")
    value = value.replace("http://doi.org/", "")
    value = value.replace("doi:", "")
    return value


def deduplicate(records):
    combined = {}

    for record in records:
        doi_key = normalize_doi(record.get("doi"))
        title_key = re.sub(
            r"\W+",
            " ",
            (record.get("title") or "").lower(),
        ).strip()

        key = doi_key or title_key
        if not key:
            continue

        if key not in combined:
            combined[key] = record.copy()
            continue

        existing = combined[key]

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

        sources = {
            item.strip()
            for item in (
                (existing.get("database", "") + "," +
                 record.get("database", "")).split(",")
            )
            if item.strip()
        }
        existing["database"] = ", ".join(sorted(sources))

        if existing.get("full_text_url"):
            existing["access_status"] = "Full-text link listed (not verified)"
        elif existing.get("abstract"):
            existing["access_status"] = "Abstract or description available"
        else:
            existing["access_status"] = "Catalogue/record link only"

    return list(combined.values())


def year_is_in_range(year, start_year, end_year):
    try:
        value = int(str(year)[:4])
        return int(start_year) <= value <= int(end_year)
    except (ValueError, TypeError):
        # Keep records with no parseable year; show the missing date clearly.
        return True


# ---------------------------------------------------------------------------
# Research-paper source searches
# ---------------------------------------------------------------------------

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
        timeout=40,
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

        location = item.get("primary_location") or {}
        venue = (location.get("source") or {}).get("display_name", "")

        oa_location = item.get("best_oa_location") or {}
        full_text_url = (
            oa_location.get("pdf_url")
            or oa_location.get("landing_page_url")
            or ""
        )

        doi = item.get("doi") or ""

        record = make_record(
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
        records.append(record)

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
        timeout=40,
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
        timeout=40,
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
        timeout=40,
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
            if pmc_id else ""
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

        if year and not year_is_in_range(year, start_year, end_year):
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
                authors=", ".join(a for a in authors if a),
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
        timeout=40,
    )
    response.raise_for_status()

    records = []

    for result in response.json().get("results", []):
        bib = result.get("bibjson", {})
        year = bib.get("year", "")

        if year and not year_is_in_range(year, start_year, end_year):
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
        timeout=40,
    )
    response.raise_for_status()

    records = []

    for item in response.json().get("data", []):
        attributes = item.get("attributes", {})
        year = (
            attributes.get("publicationYear")
            or (attributes.get("published") or "")[:4]
        )

        if year and not year_is_in_range(year, start_year, end_year):
            continue

        titles = attributes.get("titles") or []
        title = titles[0].get("title", "") if titles else ""

        creators = []
        for creator in attributes.get("creators", []):
            name = creator.get("name")
            if name:
                creators.append(name)

        doi = attributes.get("doi", "")
        doi_url = f"https://doi.org/{doi}" if doi else ""
        descriptions = attributes.get("descriptions") or []
        abstract = descriptions[0].get("description", "") if descriptions else ""

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
                record_type=", ".join(attributes.get("types", {}).values()),
                access_notes="Research output metadata; access depends on the provider.",
            )
        )

    return records


# ---------------------------------------------------------------------------
# Library of Congress official API
# ---------------------------------------------------------------------------

@st.cache_data(ttl=3600, show_spinner=False)
def search_library_of_congress(query, limit):
    response = requests.get(
        "https://www.loc.gov/search/",
        params={
            "fo": "json",
            "q": query,
            "c": limit,
        },
        headers=HEADERS,
        timeout=40,
    )
    response.raise_for_status()

    records = []

    for item in response.json().get("results", []):
        title = item.get("title", "")
        item_url = item.get("url", "")
        date_text = item.get("date", "")

        descriptions = item.get("description") or []
        if isinstance(descriptions, str):
            descriptions = [descriptions]
        description = " ".join(str(x) for x in descriptions)

        # LOC records may list digitized resources in these fields.
        resources = item.get("resources") or []
        online_formats = item.get("online_format") or []
        image_url = item.get("image_url") or []

        full_text_url = ""
        if resources:
            first_resource = resources[0]
            if isinstance(first_resource, dict):
                full_text_url = (
                    first_resource.get("url")
                    or first_resource.get("image_url")
                    or ""
                )
        elif online_formats:
            first_format = online_formats[0]
            if isinstance(first_format, dict):
                full_text_url = first_format.get("url", "")
            elif isinstance(first_format, str):
                full_text_url = first_format
        elif image_url:
            full_text_url = image_url[0] if isinstance(image_url, list) else image_url

        records.append(
            make_record(
                title=title,
                year=date_text,
                abstract=description,
                url=item_url,
                full_text_url=full_text_url,
                source="Library of Congress",
                record_type="Library/archive catalogue record",
                access_notes=(
                    "A listed online resource does not guarantee complete "
                    "digitization or access."
                ),
            )
        )

    return records


# ---------------------------------------------------------------------------
# Official catalogue links (no scraping)
# ---------------------------------------------------------------------------

def catalogue_links(query, category):
    encoded = quote(query.strip())

    if category == "Corporate & Historical Archives":
        return [
            {
                "institution": "National Archives of India — Abhilekh Patal",
                "country": "India",
                "url": "https://abhilekh-patal.in/",
                "note": (
                    "Official catalogue portal. Search availability and "
                    "digitization vary by collection."
                ),
            },
            {
                "institution": "Tata Central Archives",
                "country": "India",
                "url": "https://www.tatacentralarchives.com/",
                "note": (
                    "Institutional archive information. Check its official "
                    "site for collection access and research procedures."
                ),
            },
            {
                "institution": "National Archives Catalog (NARA)",
                "country": "United States",
                "url": (
                    "https://catalog.archives.gov/search?"
                    f"query={encoded}"
                ),
                "note": (
                    "Search the official NARA catalogue. Results may describe "
                    "records that require an archive visit or request."
                ),
            },
            {
                "institution": "Library of Congress",
                "country": "United States",
                "url": f"https://www.loc.gov/search/?q={encoded}",
                "note": "Official catalogue search. Digital access varies.",
            },
            {
                "institution": "The National Archives — Discovery",
                "country": "United Kingdom",
                "url": (
                    "https://discovery.nationalarchives.gov.uk/results/"
                    f"r?_q={encoded}"
                ),
                "note": (
                    "Official UK archive catalogue. Many records are "
                    "catalogue descriptions rather than online documents."
                ),
            },
            {
                "institution": "Archives Hub",
                "country": "United Kingdom",
                "url": (
                    "https://archiveshub.jisc.ac.uk/search/?terms="
                    f"{encoded}"
                ),
                "note": (
                    "Search descriptions of archival collections held by "
                    "participating UK repositories."
                ),
            },
            {
                "institution": "Godrej Archives",
                "country": "India",
                "url": "https://www.godrej.com/godrej-archives",
                "note": (
                    "Use the institution's official site for collection "
                    "information and access arrangements."
                ),
            },
        ]

    if category == "Archaeological Records":
        return [
            {
                "institution": "Archaeology Data Service (ADS)",
                "country": "United Kingdom",
                "url": "https://archaeologydataservice.ac.uk/",
                "note": (
                    "Official ADS site. Search and access options depend on "
                    "the dataset or archive record."
                ),
            },
            {
                "institution": "Open Context",
                "country": "International",
                "url": "https://opencontext.org/",
                "note": (
                    "Archaeological data and publication platform. Follow "
                    "its site search and record-level access terms."
                ),
            },
            {
                "institution": "tDAR — Digital Archaeological Record",
                "country": "United States / International",
                "url": "https://www.tdar.org/",
                "note": (
                    "Digital archaeological records; some materials may have "
                    "access restrictions."
                ),
            },
            {
                "institution": "Archaeological Survey of India",
                "country": "India",
                "url": "https://asi.nic.in/",
                "note": (
                    "Official ASI website. Use its published resources and "
                    "contact information for access."
                ),
            },
            {
                "institution": "Library of Congress",
                "country": "United States",
                "url": f"https://www.loc.gov/search/?q={encoded}",
                "note": (
                    "The app also searches LOC's official API for catalogue "
                    "records matching this query."
                ),
            },
        ]

    if category == "Scientific Journals & Records":
        return [
            {
                "institution": "OpenAlex",
                "country": "International",
                "url": f"https://openalex.org/works?search={encoded}",
                "note": "Scholarly works metadata and links.",
            },
            {
                "institution": "Crossref",
                "country": "International",
                "url": (
                    "https://search.crossref.org/?q="
                    f"{encoded}"
                ),
                "note": "Publication metadata and DOI records.",
            },
            {
                "institution": "DataCite",
                "country": "International",
                "url": (
                    "https://commons.datacite.org/"
                    f"?query={encoded}"
                ),
                "note": "Research datasets and other registered outputs.",
            },
        ]

    return []


# ---------------------------------------------------------------------------
# AI outline generation
# ---------------------------------------------------------------------------

def generate_outline(question, records):
    openai_key = get_secret("OPENAI_API_KEY")
    gemini_key = get_secret("GEMINI_API_KEY")

    if not openai_key and not gemini_key:
        raise ValueError(
            "Add OPENAI_API_KEY or GEMINI_API_KEY in Streamlit "
            "Settings → Secrets to generate an outline."
        )

    source_lines = []

    for index, record in enumerate(records, start=1):
        source_lines.append(
            f"[S{index}] {record.get('title', 'Untitled')}\n"
            f"Authors: {record.get('authors') or 'Not listed'}\n"
            f"Year: {record.get('year') or 'Not listed'}\n"
            f"Abstract/description: "
            f"{(record.get('abstract') or 'Not provided')[:1500]}\n"
            f"Link: {record.get('url') or 'Not listed'}\n"
            f"Access label: {record.get('access_status', '')}"
        )

    prompt = f"""
Research question: {question}

Prepare a cautious literature-review outline using only the source
information below.

Rules:
- Group relevant sources into themes.
- Cite claims with source IDs such as [S1].
- Do not invent findings, methods, authors, or citations.
- Say when the supplied abstract or description is insufficient.
- Do not claim to have read full text based only on a link.
- This is a draft outline for a researcher to verify.

Sources:
{chr(10).join(source_lines)}
"""

    # Use OpenAI if both keys are present, because it is the preferred
    # provider in this app. Gemini is used if no OpenAI key is configured.
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
                            "Use only supplied source information."
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
                        "Use only supplied source information."
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


# ---------------------------------------------------------------------------
# Citation and export helpers
# ---------------------------------------------------------------------------

def linkify_citations(text, records):
    def replace(match):
        number = int(match.group(1))
        index = number - 1

        if index < 0 or index >= len(records):
            return match.group(0)

        url = records[index].get("url")
        return f"[S{number}]({url})" if url else match.group(0)

    return re.sub(r"\[S(\d+)\]", replace, text)


def make_reference_list(records):
    lines = ["## References"]

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


def make_bibtex(records):
    entries = []

    for index, record in enumerate(records, start=1):
        authors = record.get("authors") or ""
        first_author = authors.split(",")[0] if authors else "source"
        last_name = re.sub(r"[^A-Za-z0-9]", "", first_author.split()[-1])
        year = str(record.get("year") or "nd")
        citation_key = f"{last_name or 'source'}{year}{index}"

        fields = {
            "title": record.get("title", ""),
            "author": authors,
            "year": record.get("year", ""),
            "journal": record.get("venue", ""),
            "doi": normalize_doi(record.get("doi", "")),
            "url": record.get("url", ""),
        }

        lines = [f"@misc{{{citation_key},"]
        for key, value in fields.items():
            if value:
                safe_value = str(value).replace("{", "").replace("}", "")
                lines.append(f"  {key} = {{{safe_value}}},")
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
        lines.append(f"{index}. [{title}]({url})" if url else f"{index}. {title}")
        lines.append(f"   - Date/year: {record.get('year') or 'Not listed'}")
        lines.append(f"   - Source: {record.get('database') or 'Not listed'}")
        lines.append(f"   - Access: {record.get('access_status') or 'Not listed'}")

        if record.get("full_text_url"):
            lines.append(
                f"   - Listed full-text link: {record['full_text_url']}"
            )
        if record.get("abstract"):
            lines.append(f"   - Description: {record['abstract']}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def filter_and_sort_records(records, key_prefix):
    sources = sorted({
        source.strip()
        for record in records
        for source in (record.get("database") or "").split(",")
        if source.strip()
    })

    selected_sources = st.multiselect(
        "Filter by source",
        options=sources,
        default=sources,
        key=f"{key_prefix}_sources",
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
            key=f"{key_prefix}_years",
        )

    sort_order = st.selectbox(
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

        if selected_sources and not record_sources.intersection(selected_sources):
            continue

        if year_range:
            try:
                year = int(str(record.get("year"))[:4])
                if not year_range[0] <= year <= year_range[1]:
                    continue
            except (ValueError, TypeError):
                pass

        filtered.append(record)

    if sort_order == "Newest first":
        filtered.sort(
            key=lambda item: int(str(item.get("year") or "0")[:4]),
            reverse=True,
        )
    elif sort_order == "Oldest first":
        filtered.sort(
            key=lambda item: int(str(item.get("year") or "9999")[:4])
        )
    elif sort_order == "Most cited":
        filtered.sort(
            key=lambda item: int(item.get("citations") or 0),
            reverse=True,
        )

    return filtered


def display_records(records, category_key, allow_ai=False, question=""):
    if not records:
        st.info("No records found yet. Try a search or broaden your terms.")
        return

    visible_records = filter_and_sort_records(records, category_key)
    st.write(f"Showing **{len(visible_records)}** records.")

    rows = []
    for record in visible_records:
        rows.append({
            "Title": record.get("title", ""),
            "Authors": record.get("authors", ""),
            "Date/year": record.get("year", ""),
            "Institution/venue": record.get("venue", ""),
            "Source": record.get("database", ""),
            "Access status": record.get("access_status", ""),
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
        "A listed document link is not verified. It may require sign-in, "
        "payment, an archive visit, or permission from the holding institution."
    )

    st.download_button(
        "Download CSV",
        data=pd.DataFrame(visible_records).to_csv(index=False).encode("utf-8"),
        file_name=f"{category_key}_records.csv",
        mime="text/csv",
        key=f"{category_key}_csv",
    )

    col1, col2, col3 = st.columns(3)

    with col1:
        st.download_button(
            "Download BibTeX",
            data=make_bibtex(visible_records),
            file_name=f"{category_key}_records.bib",
            mime="application/x-bibtex",
            key=f"{category_key}_bib",
        )

    with col2:
        st.download_button(
            "Download RIS",
            data=make_ris(visible_records),
            file_name=f"{category_key}_records.ris",
            mime="application/x-research-info-systems",
            key=f"{category_key}_ris",
        )

    with col3:
        outline = st.session_state.get(f"{category_key}_outline", "")
        st.download_button(
            "Download Markdown",
            data=make_markdown(question, visible_records, outline),
            file_name=f"{category_key}_research.md",
            mime="text/markdown",
            key=f"{category_key}_md",
        )

    with st.expander("View abstracts and record details"):
        for index, record in enumerate(visible_records, start=1):
            st.markdown(f"### {index}. {record.get('title', 'Untitled')}")
            st.caption(
                " · ".join(
                    item for item in [
                        record.get("authors") or "",
                        str(record.get("year") or "Date not listed"),
                        record.get("database") or "",
                        record.get("access_status") or "",
                    ]
                    if item
                )
            )

            if record.get("url"):
                st.markdown(f"[Open catalogue/record]({record['url']})")

            if record.get("full_text_url"):
                st.markdown(
                    f"[Open listed document link]({record['full_text_url']})"
                )

            if record.get("access_notes"):
                st.caption(record["access_notes"])

            st.write(
                record.get("abstract")
                or "No abstract or description was supplied."
            )
            st.divider()

    if allow_ai and visible_records:
        if st.button(
            "Generate literature-review outline",
            key=f"{category_key}_generate_outline",
        ):
            try:
                with st.spinner("Generating outline..."):
                    outline = generate_outline(question, visible_records)

                st.session_state[f"{category_key}_outline"] = outline
                st.session_state[f"{category_key}_outline_records"] = (
                    visible_records.copy()
                )
                st.rerun()

            except requests.RequestException as exc:
                st.error(f"AI service request failed: {exc}")
            except Exception as exc:
                st.error(str(exc))

        outline = st.session_state.get(f"{category_key}_outline", "")
        outline_records = st.session_state.get(
            f"{category_key}_outline_records", []
        )

        if outline:
            st.subheader("Draft literature-review outline")
            st.markdown(linkify_citations(outline, outline_records))
            st.markdown(make_reference_list(outline_records))
            st.warning(
                "Verify every claim and citation against the original "
                "source before using it in academic work."
            )


def display_catalogue_links(query, category):
    st.subheader("Official catalogue links")
    st.write(
        "These institutions are linked to their official sites. "
        "The app does not scrape these catalogues unless an approved API "
        "integration is available."
    )

    links = catalogue_links(query, category)

    for item in links:
        st.markdown(
            f"**{item['institution']}** — {item['country']}  \n"
            f"[Open official site/catalogue]({item['url']})  \n"
            f"{item['note']}"
        )


# ---------------------------------------------------------------------------
# Sidebar: save and load project
# ---------------------------------------------------------------------------

init_project_db()

with st.sidebar:
    st.header("Saved projects")

    saved_names = list_projects()

    if saved_names:
        selected_project = st.selectbox(
            "Choose a saved project",
            saved_names,
            key="saved_project_choice",
        )

        if st.button("Load project"):
            project = load_project(selected_project)
            if project:
                st.session_state["loaded_project"] = project
                st.session_state["loaded_project_name"] = selected_project
                st.rerun()

        if st.button("Delete project"):
            delete_project(selected_project)
            st.success("Project deleted.")
            st.rerun()
    else:
        st.caption("No saved projects yet.")

    st.divider()
    st.caption(
        "Projects are saved in a local SQLite file. Streamlit Community "
        "Cloud may not preserve local files after restarts or redeployments."
    )


# Restore a loaded project into session state.
loaded_project = st.session_state.get("loaded_project")
if loaded_project:
    loaded_category = loaded_project.get("category", "Research Papers")
    st.session_state[f"{loaded_category}_records"] = loaded_project.get(
        "records", []
    )
    st.session_state[f"{loaded_category}_question"] = loaded_project.get(
        "question", ""
    )
    st.session_state[f"{loaded_category}_outline"] = loaded_project.get(
        "outline", ""
    )
    st.session_state[f"{loaded_category}_outline_records"] = loaded_project.get(
        "outline_records", []
    )
    st.session_state["loaded_project"] = None


# ---------------------------------------------------------------------------
# Main UI: separate categories
# ---------------------------------------------------------------------------

st.title("📚 Research Assistant")
st.write(
    "Search research papers, historical archives, archaeological records, "
    "and scientific research outputs."
)

st.info(
    "Catalogue records and listed document links are different. A catalogue "
    "record does not mean that a document is digitized or freely accessible."
)

paper_tab, corporate_tab, archaeology_tab, science_tab = st.tabs([
    "Research Papers",
    "Corporate & Historical Archives",
    "Archaeological Records",
    "Scientific Journals & Records",
])


# ---------------------------------------------------------------------------
# Research Papers tab
# ---------------------------------------------------------------------------

with paper_tab:
    st.subheader("Research Papers")

    with st.form("paper_search_form"):
        paper_query = st.text_input(
            "Research question or topic",
            key="paper_query_input",
            placeholder="For example: effects of urban green space on health",
        )

        c1, c2, c3 = st.columns(3)
        with c1:
            paper_start = st.number_input(
                "From year",
                min_value=1800,
                max_value=2100,
                value=2015,
                key="paper_start",
            )
        with c2:
            paper_end = st.number_input(
                "To year",
                min_value=1800,
                max_value=2100,
                value=2025,
                key="paper_end",
            )
        with c3:
            paper_limit = st.slider(
                "Results per source",
                min_value=5,
                max_value=50,
                value=20,
                key="paper_limit",
            )

        paper_sources = st.multiselect(
            "Research sources",
            PAPER_SOURCES,
            default=PAPER_SOURCES,
            key="paper_sources",
        )

        paper_submitted = st.form_submit_button(
            "Search research papers",
            type="primary",
        )

    if paper_submitted:
        if not paper_query.strip():
            st.error("Enter a research topic.")
        elif paper_start > paper_end:
            st.error("The start year must not be later than the end year.")
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
            for source in paper_sources:
                try:
                    with st.spinner(f"Searching {source}..."):
                        found.extend(
                            searchers[source](
                                paper_query,
                                int(paper_start),
                                int(paper_end),
                                int(paper_limit),
                            )
                        )
                except Exception as exc:
                    st.warning(f"{source} search failed: {exc}")

            st.session_state["Research Papers_records"] = deduplicate(found)
            st.session_state["Research Papers_question"] = paper_query
            st.session_state["Research Papers_outline"] = ""
            st.session_state["Research Papers_outline_records"] = []

    paper_records = st.session_state.get("Research Papers_records", [])
    paper_question = st.session_state.get(
        "Research Papers_question",
        st.session_state.get("paper_query_input", ""),
    )

    display_records(
        paper_records,
        "Research Papers",
        allow_ai=True,
        question=paper_question,
    )


# ---------------------------------------------------------------------------
# Corporate & Historical Archives tab
# ---------------------------------------------------------------------------

with corporate_tab:
    st.subheader("Corporate & Historical Archives")
    st.write(
        "Search the Library of Congress through its API, and use the official "
        "catalogue links for other archive institutions. The app does not "
        "scrape catalogue websites."
    )

    with st.form("corporate_search_form"):
        corporate_query = st.text_input(
            "Company, organization, person, or topic",
            key="corporate_query_input",
            placeholder="For example: Tata Iron and Steel Company",
        )
        corporate_limit = st.slider(
            "Library of Congress results",
            min_value=5,
            max_value=50,
            value=20,
            key="corporate_limit",
        )
        corporate_submitted = st.form_submit_button(
            "Search archives",
            type="primary",
        )

    if corporate_submitted:
        if not corporate_query.strip():
            st.error("Enter a company, organization, person, or topic.")
        else:
            try:
                with st.spinner("Searching Library of Congress..."):
                    loc_records = search_library_of_congress(
                        corporate_query,
                        int(corporate_limit),
                    )

                st.session_state[
                    "Corporate & Historical Archives_records"
                ] = loc_records
                st.session_state[
                    "Corporate & Historical Archives_question"
                ] = corporate_query

            except Exception as exc:
                st.warning(f"Library of Congress search failed: {exc}")

    corporate_records = st.session_state.get(
        "Corporate & Historical Archives_records",
        [],
    )
    corporate_question = st.session_state.get(
        "Corporate & Historical Archives_question",
        st.session_state.get("corporate_query_input", ""),
    )

    st.markdown("### Direct API results")
    if corporate_records:
        display_records(
            corporate_records,
            "Corporate Archives",
            question=corporate_question,
        )
    else:
        st.caption(
            "No direct API results loaded. Use the official catalogue links "
            "below to search archive holdings."
        )

    if corporate_question.strip():
        display_catalogue_links(
            corporate_question,
            "Corporate & Historical Archives",
        )


# ---------------------------------------------------------------------------
# Archaeological Records tab
# ---------------------------------------------------------------------------

with archaeology_tab:
    st.subheader("Archaeological Records")
    st.write(
        "This section provides official institutional catalogue links. "
        "The listed archaeology services are not automatically searched "
        "unless an approved API integration is available."
    )

    with st.form("archaeology_search_form"):
        archaeology_query = st.text_input(
            "Site, region, period, project, or topic",
            key="archaeology_query_input",
            placeholder="For example: Indus Valley excavation",
        )
        archaeology_submitted = st.form_submit_button(
            "Show archaeology catalogues",
            type="primary",
        )

    if archaeology_submitted:
        if not archaeology_query.strip():
            st.error("Enter a site, region, period, project, or topic.")
        else:
            st.session_state["Archaeological Records_question"] = (
                archaeology_query
            )

    archaeology_question = st.session_state.get(
        "Archaeological Records_question",
        st.session_state.get("archaeology_query_input", ""),
    )

    if archaeology_question.strip():
        display_catalogue_links(
            archaeology_question,
            "Archaeological Records",
        )


# ---------------------------------------------------------------------------
# Scientific Journals & Records tab
# ---------------------------------------------------------------------------

with science_tab:
    st.subheader("Scientific Journals & Records")
    st.write(
        "Search scholarly articles and research outputs separately from "
        "archive and archaeology catalogue records."
    )

    with st.form("science_search_form"):
        science_query = st.text_input(
            "Scientific topic or research-output query",
            key="science_query_input",
            placeholder="For example: climate change crop yield dataset",
        )
        science_start = st.number_input(
            "From year",
            min_value=1800,
            max_value=2100,
            value=2015,
            key="science_start",
        )
        science_end = st.number_input(
            "To year",
            min_value=1800,
            max_value=2100,
            value=2025,
            key="science_end",
        )
        science_limit = st.slider(
            "Results per source",
            min_value=5,
            max_value=50,
            value=20,
            key="science_limit",
        )
        science_sources = st.multiselect(
            "Scientific sources",
            PAPER_SOURCES,
            default=["OpenAlex", "Crossref", "PubMed", "Europe PMC", "DataCite"],
            key="science_sources",
        )
        science_submitted = st.form_submit_button(
            "Search scientific records",
            type="primary",
        )

    if science_submitted:
        if not science_query.strip():
            st.error("Enter a scientific topic or query.")
        elif science_start > science_end:
            st.error("The start year must not be later than the end year.")
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
            for source in science_sources:
                try:
                    with st.spinner(f"Searching {source}..."):
                        found.extend(
                            searchers[source](
                                science_query,
                                int(science_start),
                                int(science_end),
                                int(science_limit),
                            )
                        )
                except Exception as exc:
                    st.warning(f"{source} search failed: {exc}")

            st.session_state[
                "Scientific Journals & Records_records"
            ] = deduplicate(found)
            st.session_state[
                "Scientific Journals & Records_question"
            ] = science_query
            st.session_state[
                "Scientific Journals & Records_outline"
            ] = ""
            st.session_state[
                "Scientific Journals & Records_outline_records"
            ] = []

    science_records = st.session_state.get(
        "Scientific Journals & Records_records",
        [],
    )
    science_question = st.session_state.get(
        "Scientific Journals & Records_question",
        st.session_state.get("science_query_input", ""),
    )

    display_records(
        science_records,
        "Scientific Journals & Records",
        allow_ai=True,
        question=science_question,
    )

    if science_question.strip():
        display_catalogue_links(
            science_question,
            "Scientific Journals & Records",
        )


# ---------------------------------------------------------------------------
# Save current project
# ---------------------------------------------------------------------------

st.divider()
st.subheader("Save current project")

category_to_save = st.selectbox(
    "Choose which section to save",
    [
        "Research Papers",
        "Corporate & Historical Archives",
        "Archaeological Records",
        "Scientific Journals & Records",
    ],
    key="save_category_choice",
)

project_name = st.text_input(
    "Project name",
    value=st.session_state.get("loaded_project_name", ""),
    key="save_project_name",
)

if st.button("Save project"):
    records_key = f"{category_to_save}_records"
    question_key = f"{category_to_save}_question"
    outline_key = f"{category_to_save}_outline"
    outline_records_key = f"{category_to_save}_outline_records"

    payload = {
        "category": category_to_save,
        "question": st.session_state.get(question_key, ""),
        "records": st.session_state.get(records_key, []),
        "outline": st.session_state.get(outline_key, ""),
        "outline_records": st.session_state.get(
            outline_records_key, []
        ),
    }

    if not project_name.strip():
        st.error("Enter a project name.")
    elif not payload["question"] and not payload["records"]:
        st.error("Search for something before saving a project.")
    else:
        save_project(project_name.strip(), payload)
        st.session_state["loaded_project_name"] = project_name.strip()
        st.success(f"Saved project: {project_name.strip()}")
