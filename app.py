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

HEADERS = {
    "User-Agent": "ResearchAssistant/1.0"
}

DEFAULT_SOURCES = [
    "OpenAlex",
    "Crossref",
    "PubMed",
    "Europe PMC",
    "arXiv",
    "DOAJ",
    "OpenAIRE",
]

DB_PATH = "research_projects.db"


# ---------------------------------------------------------------------------
# Secrets and database helpers
# ---------------------------------------------------------------------------

def get_secret(name):
    """Read a key from Streamlit secrets or environment variables."""
    try:
        value = st.secrets.get(name, "")
    except Exception:
        value = ""

    return value or os.getenv(name, "")


def init_project_db():
    """Create the local SQLite database and projects table if needed."""
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


def save_project(name, question, records, outline="", outline_records=None):
    init_project_db()

    payload = {
        "question": question,
        "records": records,
        "outline": outline,
        "outline_records": outline_records or [],
    }
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

    if not row:
        return None

    return json.loads(row[0])


def delete_project(name):
    init_project_db()

    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            "DELETE FROM projects WHERE name = ?",
            (name,),
        )
        connection.commit()


# ---------------------------------------------------------------------------
# General helpers
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
    year=None,
    abstract="",
    url="",
    full_text_url="",
    venue="",
    database="",
    doi="",
):
    abstract = clean_text(abstract)

    if full_text_url:
        access_status = "Full-text link listed (not verified)"
    elif abstract:
        access_status = "Abstract available"
    else:
        access_status = "Record only"

    return {
        "title": clean_text(title) or "Untitled",
        "authors": clean_text(authors),
        "year": year or "",
        "abstract": abstract,
        "doi": doi or "",
        "url": url or "",
        "full_text_url": full_text_url or "",
        "access_status": access_status,
        "venue": clean_text(venue),
        "citations": "",
        "type": "",
        "database": database or "",
    }


def reconstruct_openalex_abstract(inverted_index):
    """Convert OpenAlex's position-indexed abstract into normal text."""
    if not inverted_index:
        return ""

    words_by_position = {}

    for word, positions in inverted_index.items():
        for position in positions:
            words_by_position[position] = word

    return " ".join(
        words_by_position[position]
        for position in sorted(words_by_position)
    )


def get_openalex_authors(authorships):
    names = []

    for authorship in authorships or []:
        author = authorship.get("author") or {}
        name = author.get("display_name")
        if name:
            names.append(name)

    return ", ".join(names)


def normalize_doi(value):
    value = (value or "").strip().lower()
    value = value.replace("https://doi.org/", "")
    value = value.replace("http://doi.org/", "")
    value = value.replace("doi:", "")
    return value


def combine_and_deduplicate(records):
    """Combine duplicate records, retaining useful information."""
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
            "doi",
            "url",
            "full_text_url",
            "venue",
            "type",
            "citations",
        ]:
            if not existing.get(field) and record.get(field):
                existing[field] = record[field]

        if len(record.get("abstract", "")) > len(existing.get("abstract", "")):
            existing["abstract"] = record["abstract"]

        databases = {
            source.strip()
            for source in (
                (existing.get("database", "") + "," +
                 record.get("database", "")).split(",")
            )
            if source.strip()
        }
        existing["database"] = ", ".join(sorted(databases))

        if existing.get("full_text_url"):
            existing["access_status"] = "Full-text link listed (not verified)"
        elif existing.get("abstract"):
            existing["access_status"] = "Abstract available"
        else:
            existing["access_status"] = "Record only"

    return list(combined.values())


# ---------------------------------------------------------------------------
# OpenAlex search
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
        primary_location = item.get("primary_location") or {}
        source = primary_location.get("source") or {}
        open_access_location = item.get("best_oa_location") or {}

        full_text_url = (
            open_access_location.get("pdf_url")
            or open_access_location.get("landing_page_url")
            or ""
        )

        doi = item.get("doi") or ""

        record = make_record(
            title=item.get("title"),
            authors=get_openalex_authors(item.get("authorships")),
            year=item.get("publication_year"),
            abstract=reconstruct_openalex_abstract(
                item.get("abstract_inverted_index")
            ),
            url=doi or item.get("id") or "",
            full_text_url=full_text_url,
            venue=source.get("display_name", ""),
            database="OpenAlex",
            doi=doi,
        )

        record["citations"] = item.get("cited_by_count", "")
        record["type"] = item.get("type", "")
        records.append(record)

    return records


# ---------------------------------------------------------------------------
# Crossref search
# ---------------------------------------------------------------------------

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
                part
                for part in [
                    author.get("given", ""),
                    author.get("family", ""),
                ]
                if part
            )
            if name:
                authors.append(name)

        date_parts = (
            item.get("published", {}).get("date-parts")
            or item.get("created", {}).get("date-parts")
            or [[]]
        )
        year = (
            date_parts[0][0]
            if date_parts and date_parts[0]
            else ""
        )

        doi = item.get("DOI", "")
        doi_url = f"https://doi.org/{doi}" if doi else ""

        full_text_url = ""
        for link in item.get("link", []):
            content_type = (link.get("content-type") or "").lower()
            if "pdf" in content_type and link.get("URL"):
                full_text_url = link["URL"]
                break

        record = make_record(
            title=titles[0] if titles else "Untitled",
            authors=", ".join(authors),
            year=year,
            abstract=item.get("abstract", ""),
            url=doi_url,
            full_text_url=full_text_url,
            venue=(item.get("container-title") or [""])[0],
            database="Crossref",
            doi=doi_url,
        )

        record["type"] = item.get("type", "")
        records.append(record)

    return records


# ---------------------------------------------------------------------------
# PubMed search
# ---------------------------------------------------------------------------

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
                abstract_parts.append(
                    f"{label}: {text}" if label else text
                )

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
                database="PubMed",
            )
        )

    return records


# ---------------------------------------------------------------------------
# Europe PMC search
# ---------------------------------------------------------------------------

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

        if pmcid:
            record_url = f"https://europepmc.org/articles/{pmcid}"
        else:
            record_url = (
                f"https://europepmc.org/article/{source}/{paper_id}"
            )

        full_text_url = ""
        links = (
            item.get("fullTextUrlList", {})
            .get("fullTextUrl", [])
        )

        if isinstance(links, dict):
            links = [links]

        for link in links:
            if link.get("url"):
                full_text_url = link["url"]
                break

        records.append(
            make_record(
                title=item.get("title"),
                authors=item.get("authorString", ""),
                year=(item.get("firstPublicationDate") or "")[:4],
                abstract=item.get("abstractText", ""),
                url=record_url,
                full_text_url=full_text_url,
                venue=item.get("journalTitle", ""),
                database="Europe PMC",
            )
        )

    return records


# ---------------------------------------------------------------------------
# arXiv search
# ---------------------------------------------------------------------------

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
                "atom:title",
                default="",
                namespaces=namespace,
            ).split()
        )

        abstract = " ".join(
            entry.findtext(
                "atom:summary",
                default="",
                namespaces=namespace,
            ).split()
        )

        record_url = entry.findtext(
            "atom:id",
            default="",
            namespaces=namespace,
        )

        published = entry.findtext(
            "atom:published",
            default="",
            namespaces=namespace,
        )
        year = published[:4] if published else ""

        if year:
            try:
                if not int(start_year) <= int(year) <= int(end_year):
                    continue
            except ValueError:
                pass

        authors = [
            author.findtext(
                "atom:name",
                default="",
                namespaces=namespace,
            )
            for author in entry.findall("atom:author", namespace)
        ]

        full_text_url = ""
        if record_url:
            full_text_url = record_url.replace("/abs/", "/pdf/")
            if not full_text_url.endswith(".pdf"):
                full_text_url += ".pdf"

        records.append(
            make_record(
                title=title,
                authors=", ".join(name for name in authors if name),
                year=year,
                abstract=abstract,
                url=record_url,
                full_text_url=full_text_url,
                database="arXiv",
            )
        )

        if len(records) >= limit:
            break

    return records


# ---------------------------------------------------------------------------
# DOAJ search
# ---------------------------------------------------------------------------

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

        if year:
            try:
                if not int(start_year) <= int(year) <= int(end_year):
                    continue
            except (TypeError, ValueError):
                pass

        authors = [
            author.get("name", "")
            for author in bib.get("author", [])
            if author.get("name")
        ]

        links = bib.get("link", [])
        full_text_url = ""

        for link in links:
            link_type = (link.get("type") or "").lower()
            if "fulltext" in link_type and link.get("url"):
                full_text_url = link["url"]
                break

        record_url = full_text_url
        if not record_url and links:
            record_url = links[0].get("url", "")

        journal = bib.get("journal", {}).get("title", "")
        doi_value = ""

        for identifier in bib.get("identifier", []):
            if identifier.get("type", "").lower() == "doi":
                doi_value = identifier.get("id", "")
                break

        doi_url = (
            f"https://doi.org/{doi_value}"
            if doi_value
            else ""
        )

        records.append(
            make_record(
                title=bib.get("title"),
                authors=", ".join(authors),
                year=year,
                abstract=bib.get("abstract", ""),
                url=record_url or doi_url,
                full_text_url=full_text_url,
                venue=journal,
                database="DOAJ",
                doi=doi_url,
            )
        )

    return records


# ---------------------------------------------------------------------------
# OpenAIRE search
# ---------------------------------------------------------------------------

def xml_local_name(tag):
    """Remove XML namespace from a tag."""
    return tag.rsplit("}", 1)[-1].lower()


def find_xml_text(element, wanted_names):
    wanted_names = {name.lower() for name in wanted_names}

    for child in element.iter():
        if xml_local_name(child.tag) in wanted_names and child.text:
            text = child.text.strip()
            if text:
                return text

    return ""


@st.cache_data(ttl=3600, show_spinner=False)
def search_openaire(query, start_year, end_year, limit):
    response = requests.get(
        "https://api.openaire.eu/search/publications",
        params={
            "title": query,
            "fromDateAccepted": f"{start_year}-01-01",
            "toDateAccepted": f"{end_year}-12-31",
            "size": limit,
            "format": "xml",
        },
        headers=HEADERS,
        timeout=50,
    )
    response.raise_for_status()

    root = ET.fromstring(response.text)
    records = []

    for element in root.iter():
        if xml_local_name(element.tag) != "result":
            continue

        title = find_xml_text(element, {"title"})
        if not title:
            continue

        authors = []
        for child in element.iter():
            if xml_local_name(child.tag) in {"creator", "author"} and child.text:
                name = child.text.strip()
                if name and name not in authors:
                    authors.append(name)

        date_text = find_xml_text(
            element,
            {"dateofacceptance", "publicationdate", "date"},
        )
        year_match = re.search(r"\b(?:19|20)\d{2}\b", date_text or "")
        year = year_match.group(0) if year_match else ""

        listed_url = ""
        full_text_url = ""

        for child in element.iter():
            tag = xml_local_name(child.tag)
            if tag in {"webresource", "url"} and child.text:
                candidate = child.text.strip()
                if candidate.startswith("http"):
                    listed_url = listed_url or candidate
                    if tag == "webresource":
                        full_text_url = candidate

        doi = ""
        for child in element.iter():
            if xml_local_name(child.tag) == "pid" and child.text:
                pid_type = (child.attrib.get("class") or "").lower()
                pid_value = child.text.strip()

                if "doi" in pid_type or pid_value.lower().startswith("10."):
                    doi = (
                        pid_value
                        if pid_value.startswith("http")
                        else f"https://doi.org/{pid_value}"
                    )
                    break

        record_url = (
            doi
            or listed_url
            or (
                "https://explore.openaire.eu/search/publications?"
                f"query={quote(title)}"
            )
        )

        records.append(
            make_record(
                title=title,
                authors=", ".join(authors),
                year=year,
                abstract=find_xml_text(element, {"description"}),
                url=record_url,
                full_text_url=full_text_url,
                database="OpenAIRE",
                doi=doi,
            )
        )

        if len(records) >= limit:
            break

    return records


# ---------------------------------------------------------------------------
# AI outline generation: Gemini preferred, OpenAI as fallback
# ---------------------------------------------------------------------------

def generate_outline(question, records):
    gemini_key = get_secret("GEMINI_API_KEY")
    openai_key = get_secret("OPENAI_API_KEY")

    if not gemini_key and not openai_key:
        raise ValueError(
            "Add GEMINI_API_KEY or OPENAI_API_KEY to Streamlit secrets "
            "to generate an AI outline."
        )

    source_text = []

    for index, record in enumerate(records, start=1):
        source_text.append(
            f"[S{index}] {record.get('title', 'Untitled')}\n"
            f"Authors: {record.get('authors') or 'Not listed'}\n"
            f"Year: {record.get('year') or 'Not listed'}\n"
            f"Abstract: "
            f"{(record.get('abstract') or 'No abstract available')[:1800]}\n"
            f"Source link: {record.get('url') or 'No link listed'}\n"
            f"Access label: {record.get('access_status', 'Record only')}"
        )

    prompt = f"""
Research question:
{question}

Create a literature-review outline using only the source information below.

Requirements:
- Group relevant sources into themes.
- Cite sources using IDs such as [S1] and [S2].
- Do not invent findings, methods, authors, or citations.
- Say when an abstract does not provide enough information.
- A listed full-text link is unverified. Do not claim you read the full text.
- This is an outline for a human researcher to verify, not a finished paper.

Sources:
{chr(10).join(source_text)}
"""

    # Prefer Gemini when its key is available.
    if gemini_key:
        response = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/"
            "models/gemini-2.5-flash:generateContent",
            params={"key": gemini_key},
            json={
                "systemInstruction": {
                    "parts": [
                        {
                            "text": (
                                "You are a cautious research assistant. "
                                "Use only the supplied source information. "
                                "Never invent evidence or citations."
                            )
                        }
                    ]
                },
                "contents": [
                    {
                        "parts": [{"text": prompt}]
                    }
                ],
                "generationConfig": {
                    "temperature": 0.2
                },
            },
            timeout=90,
        )
        response.raise_for_status()

        data = response.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]

    # If there is no Gemini key, use OpenAI.
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
                        "Use only supplied source information and never "
                        "invent evidence or citations."
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


# ---------------------------------------------------------------------------
# Citation and export helpers
# ---------------------------------------------------------------------------

def linkify_source_citations(text, records):
    """Turn [S1] citations into clickable links to their records."""
    def replace(match):
        source_number = int(match.group(1))
        index = source_number - 1

        if index < 0 or index >= len(records):
            return match.group(0)

        url = records[index].get("url")
        if not url:
            return match.group(0)

        return f"[S{source_number}]({url})"

    return re.sub(r"\[S(\d+)\]", replace, text)


def make_reference_list(records):
    lines = ["## References"]

    for index, record in enumerate(records, start=1):
        authors = record.get("authors") or "Authors not listed"
        year = record.get("year") or "Year not listed"
        title = record.get("title") or "Untitled"
        venue = record.get("venue") or ""
        url = record.get("url") or ""

        reference = f"[S{index}] {authors} ({year}). *{title}*."
        if venue:
            reference += f" {venue}."
        if url:
            reference += f" [Open source]({url})"

        lines.append(reference)

    return "\n".join(lines)


def bibtex_escape(value):
    value = str(value or "")
    return (
        value.replace("\\", r"\textbackslash{}")
        .replace("&", r"\&")
        .replace("%", r"\%")
        .replace("$", r"\$")
        .replace("#", r"\#")
        .replace("_", r"\_")
        .replace("{", r"\{")
        .replace("}", r"\}")
    )


def make_bibtex(records):
    entries = []

    for index, record in enumerate(records, start=1):
        title = record.get("title") or "Untitled"
        authors = record.get("authors") or ""
        year = str(record.get("year") or "")

        first_author = authors.split(",")[0] if authors else "source"
        key_author = re.sub(
            r"[^A-Za-z0-9]",
            "",
            first_author.split()[-1],
        )
        citation_key = f"{key_author or 'source'}{year or 'nd'}{index}"

        fields = {
            "title": title,
            "author": authors,
            "year": year,
            "journal": record.get("venue", ""),
            "doi": normalize_doi(record.get("doi", "")),
            "url": record.get("url", ""),
        }

        lines = [f"@misc{{{citation_key},"]
        for field, value in fields.items():
            if value:
                lines.append(
                    f"  {field} = {{{bibtex_escape(value)}}},"
                )
        lines.append("}")
        entries.append("\n".join(lines))

    return "\n\n".join(entries)


def make_ris(records):
    entries = []

    for record in records:
        lines = ["TY  - JOUR"]

        if record.get("title"):
            lines.append(f"TI  - {record['title']}")

        for author in (record.get("authors") or "").split(","):
            author = author.strip()
            if author:
                lines.append(f"AU  - {author}")

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


def make_markdown_export(question, records, outline=""):
    lines = [
        f"# Research project: {question}",
        "",
    ]

    if outline:
        lines.extend([outline, ""])

    lines.append("## Sources")

    for index, record in enumerate(records, start=1):
        title = record.get("title") or "Untitled"
        url = record.get("url") or ""
        authors = record.get("authors") or "Authors not listed"
        year = record.get("year") or "Year not listed"
        access = record.get("access_status") or "Record only"

        if url:
            lines.append(f"{index}. [{title}]({url})")
        else:
            lines.append(f"{index}. {title}")

        lines.append(f"   - Authors: {authors}")
        lines.append(f"   - Year: {year}")
        lines.append(f"   - Found in: {record.get('database', '')}")
        lines.append(f"   - Access: {access}")

        if record.get("full_text_url"):
            lines.append(
                f"   - Listed full-text link: {record['full_text_url']}"
            )

        if record.get("abstract"):
            lines.append(f"   - Abstract: {record['abstract']}")

        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Load saved-project controls before creating the search form
# ---------------------------------------------------------------------------

init_project_db()

with st.sidebar:
    st.header("Saved projects")

    project_names = list_projects()

    if project_names:
        chosen_project = st.selectbox(
            "Choose a project",
            options=project_names,
            key="load_project_selection",
        )

        if st.button("Load project"):
            project = load_project(chosen_project)

            if project:
                st.session_state["question_input"] = project.get(
                    "question", ""
                )
                st.session_state["records"] = project.get("records", [])
                st.session_state["question"] = project.get("question", "")
                st.session_state["outline"] = project.get("outline", "")
                st.session_state["outline_records"] = project.get(
                    "outline_records", []
                )
                st.session_state["active_project"] = chosen_project
                st.rerun()

        if st.button("Delete project"):
            delete_project(chosen_project)
            st.success("Project deleted.")
            st.rerun()
    else:
        st.caption("No saved projects yet.")

    st.divider()
    st.caption(
        "Saved projects use a local SQLite file. For dependable cloud "
        "storage, connect a hosted database."
    )


# ---------------------------------------------------------------------------
# Main interface and search
# ---------------------------------------------------------------------------

st.title("📚 Research Assistant")
st.write(
    "Search scholarly sources, review records and links, organize papers, "
    "and create a source-linked literature-review outline."
)

st.info(
    "A 'Full-text link listed' label means a provider supplied a possible "
    "full-text link. The app does not verify that the link works or that "
    "you have access."
)

if "question_input" not in st.session_state:
    st.session_state["question_input"] = ""

with st.form("research_search"):
    question = st.text_input(
        "Research topic or question",
        key="question_input",
        placeholder=(
            "For example: How does urban green space affect mental health?"
        ),
    )

    col1, col2, col3 = st.columns(3)

    with col1:
        start_year = st.number_input(
            "From year",
            min_value=1800,
            max_value=2100,
            value=2015,
        )

    with col2:
        end_year = st.number_input(
            "To year",
            min_value=1800,
            max_value=2100,
            value=2025,
        )

    with col3:
        limit = st.slider(
            "Maximum results per source",
            min_value=5,
            max_value=50,
            value=20,
        )

    selected_sources = st.multiselect(
        "Sources to search",
        options=DEFAULT_SOURCES,
        default=DEFAULT_SOURCES,
    )

    submitted = st.form_submit_button(
        "Search research sources",
        type="primary",
    )


if submitted:
    if not question.strip():
        st.error("Enter a research topic or question.")
    elif start_year > end_year:
        st.error("The start year must not be later than the end year.")
    elif not selected_sources:
        st.error("Select at least one source.")
    else:
        search_functions = {
            "OpenAlex": lambda: search_openalex(
                question, int(start_year), int(end_year), int(limit)
            ),
            "Crossref": lambda: search_crossref(
                question, int(start_year), int(end_year), int(limit)
            ),
            "PubMed": lambda: search_pubmed(
                question, int(start_year), int(end_year), int(limit)
            ),
            "Europe PMC": lambda: search_europe_pmc(
                question, int(start_year), int(end_year), int(limit)
            ),
            "arXiv": lambda: search_arxiv(
                question, int(start_year), int(end_year), int(limit)
            ),
            "DOAJ": lambda: search_doaj(
                question, int(start_year), int(end_year), int(limit)
            ),
            "OpenAIRE": lambda: search_openaire(
                question, int(start_year), int(end_year), int(limit)
            ),
        }

        found_records = []

        for source in selected_sources:
            try:
                with st.spinner(f"Searching {source}..."):
                    found_records.extend(search_functions[source]())
            except requests.RequestException as exc:
                st.warning(f"{source} search failed: {exc}")
            except Exception as exc:
                st.warning(f"{source} could not be searched: {exc}")

        st.session_state["records"] = combine_and_deduplicate(found_records)
        st.session_state["question"] = question
        st.session_state["outline"] = ""
        st.session_state["outline_records"] = []


# ---------------------------------------------------------------------------
# Results, filters, sorting, downloads, and saved-project tools
# ---------------------------------------------------------------------------

if "records" in st.session_state:
    all_records = st.session_state["records"]

    st.subheader(f"Search results: {len(all_records)} unique records")

    if not all_records:
        st.info(
            "No results were returned. Try different wording, a wider date "
            "range, or different sources."
        )
    else:
        st.subheader("Filter and sort results")

        available_sources = sorted({
            source.strip()
            for record in all_records
            for source in (record.get("database") or "").split(",")
            if source.strip()
        })

        selected_filter_sources = st.multiselect(
            "Filter by source",
            options=available_sources,
            default=available_sources,
        )

        available_years = []
        for record in all_records:
            try:
                available_years.append(int(record.get("year")))
            except (TypeError, ValueError):
                pass

        year_range = None
        if available_years:
            minimum_year = min(available_years)
            maximum_year = max(available_years)

            if minimum_year == maximum_year:
                st.caption(f"Publication year: {minimum_year}")
                year_range = (minimum_year, maximum_year)
            else:
                year_range = st.slider(
                    "Filter by publication year",
                    min_value=minimum_year,
                    max_value=maximum_year,
                    value=(minimum_year, maximum_year),
                )

        sort_by = st.selectbox(
            "Sort results by",
            [
                "Relevance/order found",
                "Newest first",
                "Oldest first",
                "Most cited",
            ],
        )

        filtered_records = []

        for record in all_records:
            record_sources = {
                source.strip()
                for source in (record.get("database") or "").split(",")
                if source.strip()
            }

            if (
                selected_filter_sources
                and not record_sources.intersection(selected_filter_sources)
            ):
                continue

            if year_range:
                try:
                    record_year = int(record.get("year"))
                    if not year_range[0] <= record_year <= year_range[1]:
                        continue
                except (TypeError, ValueError):
                    # Keep records with missing/unclear years.
                    pass

            filtered_records.append(record)

        if sort_by == "Newest first":
            filtered_records.sort(
                key=lambda item: int(item.get("year") or 0),
                reverse=True,
            )
        elif sort_by == "Oldest first":
            filtered_records.sort(
                key=lambda item: int(item.get("year") or 9999),
            )
        elif sort_by == "Most cited":
            filtered_records.sort(
                key=lambda item: int(item.get("citations") or 0),
                reverse=True,
            )

        records = filtered_records

        st.write(f"Showing **{len(records)}** records after filtering.")

        display_rows = []

        for record in records:
            display_rows.append(
                {
                    "Title": record.get("title", ""),
                    "Authors": record.get("authors", ""),
                    "Year": record.get("year", ""),
                    "Venue": record.get("venue", ""),
                    "Found in": record.get("database", ""),
                    "Access status": record.get("access_status", ""),
                    "Record link": record.get("url", ""),
                    "Full-text link": record.get("full_text_url", ""),
                }
            )

        st.dataframe(
            pd.DataFrame(display_rows),
            use_container_width=True,
            hide_index=True,
            column_config={
                "Record link": st.column_config.LinkColumn("Record link"),
                "Full-text link": st.column_config.LinkColumn(
                    "Full-text link"
                ),
            },
        )

        st.caption(
            "Search results and abstracts are supplied by the listed "
            "research services. Check original sources before citing."
        )

        # CSV, BibTeX, RIS, and Markdown downloads.
        csv_data = pd.DataFrame(records).to_csv(index=False).encode("utf-8")

        download_col1, download_col2, download_col3, download_col4 = (
            st.columns(4)
        )

        with download_col1:
            st.download_button(
                "Download CSV",
                data=csv_data,
                file_name="research_sources.csv",
                mime="text/csv",
            )

        with download_col2:
            st.download_button(
                "Download BibTeX",
                data=make_bibtex(records),
                file_name="research_sources.bib",
                mime="application/x-bibtex",
            )

        with download_col3:
            st.download_button(
                "Download RIS",
                data=make_ris(records),
                file_name="research_sources.ris",
                mime="application/x-research-info-systems",
            )

        outline = st.session_state.get("outline", "")
        markdown_data = make_markdown_export(
            st.session_state.get("question", "Research project"),
            records,
            outline,
        )

        with download_col4:
            st.download_button(
                "Download Markdown",
                data=markdown_data,
                file_name="research_project.md",
                mime="text/markdown",
            )

        # Review abstracts and links.
        with st.expander("Review abstracts and source details"):
            for index, record in enumerate(records, start=1):
                st.markdown(
                    f"### [{index}] {record.get('title', 'Untitled')}"
                )

                details = [
                    record.get("authors") or "Authors not listed",
                    str(record.get("year") or "Year not listed"),
                    record.get("database") or "Source not listed",
                    record.get("access_status") or "Record only",
                ]
                st.caption(" · ".join(details))

                if record.get("url"):
                    st.markdown(f"[Open record]({record['url']})")

                if record.get("full_text_url"):
                    st.markdown(
                        "[Open listed full-text link]"
                        f"({record['full_text_url']})"
                    )

                st.write(
                    record.get("abstract")
                    or "No abstract was provided by this source."
                )
                st.divider()

        # AI outline generation uses the currently filtered/sorted records.
        if st.button("Generate literature-review outline"):
            if not records:
                st.warning("There are no visible records to use.")
            else:
                try:
                    with st.spinner("Creating a draft outline..."):
                        generated_outline = generate_outline(
                            st.session_state.get("question", ""),
                            records,
                        )

                    st.session_state["outline"] = generated_outline
                    st.session_state["outline_records"] = records.copy()
                    st.rerun()

                except requests.RequestException as exc:
                    st.error(f"The AI service request failed: {exc}")
                except Exception as exc:
                    st.error(str(exc))

        # Show the outline and references, using the exact source order
        # that was sent to the AI.
        saved_outline = st.session_state.get("outline", "")
        outline_records = st.session_state.get("outline_records", [])

        if saved_outline:
            st.subheader("Draft literature-review outline")
            st.markdown(
                linkify_source_citations(saved_outline, outline_records)
            )
            st.markdown(make_reference_list(outline_records))
            st.warning(
                "Verify every claim and citation against the original "
                "source before using it in academic work."
            )

        # Save the project.
        st.subheader("Save this project")

        project_name = st.text_input(
            "Project name",
            value=st.session_state.get("active_project", ""),
            key="project_name_input",
        )

        if st.button("Save project"):
            if not project_name.strip():
                st.error("Enter a project name first.")
            else:
                save_project(
                    name=project_name.strip(),
                    question=st.session_state.get("question", ""),
                    records=all_records,
                    outline=st.session_state.get("outline", ""),
                    outline_records=st.session_state.get(
                        "outline_records", []
                    ),
                )
                st.session_state["active_project"] = project_name.strip()
                st.success(f"Saved project: {project_name.strip()}")
