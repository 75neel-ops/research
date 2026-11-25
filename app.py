import html
import os
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote

import pandas as pd
import requests
import streamlit as st


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Research Assistant",
    page_icon="📚",
    layout="wide",
)

HEADERS = {
    "User-Agent": "ResearchAssistant/1.0 (scholarly search app)"
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

ALL_SOURCES = DEFAULT_SOURCES + ["CORE"]


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------

def get_secret(name):
    """Read a key from Streamlit secrets, falling back to environment variables."""
    try:
        value = st.secrets.get(name, "")
    except Exception:
        value = ""

    return value or os.getenv(name, "")


def clean_text(value):
    """Remove basic HTML and normalize whitespace."""
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
    """Create a consistent record format for all search providers."""
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
    """Rebuild OpenAlex's position-indexed abstract into normal text."""
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
    """Deduplicate results while retaining useful data from different providers."""
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

        # Fill missing fields from the duplicate record.
        for field in [
            "authors",
            "year",
            "abstract",
            "doi",
            "url",
            "full_text_url",
            "venue",
            "type",
        ]:
            if not existing.get(field) and record.get(field):
                existing[field] = record[field]

        # Keep the longest abstract if both providers supplied one.
        if len(record.get("abstract", "")) > len(existing.get("abstract", "")):
            existing["abstract"] = record["abstract"]

        # Preserve the names of all databases where this work was found.
        databases = set(
            item.strip()
            for item in (
                (existing.get("database", "") + "," + record.get("database", ""))
            ).split(",")
            if item.strip()
        )
        existing["database"] = ", ".join(sorted(databases))

        if existing.get("full_text_url"):
            existing["access_status"] = "Full-text link listed (not verified)"
        elif existing.get("abstract"):
            existing["access_status"] = "Abstract available"
        else:
            existing["access_status"] = "Record only"

    return list(combined.values())


# ---------------------------------------------------------------------------
# OpenAlex
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
        timeout=30,
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
        record_url = doi or item.get("id") or ""

        records.append(
            make_record(
                title=item.get("title"),
                authors=get_openalex_authors(item.get("authorships")),
                year=item.get("publication_year"),
                abstract=reconstruct_openalex_abstract(
                    item.get("abstract_inverted_index")
                ),
                url=record_url,
                full_text_url=full_text_url,
                venue=source.get("display_name", ""),
                database="OpenAlex",
                doi=doi,
            )
        )

        records[-1]["citations"] = item.get("cited_by_count", "")
        records[-1]["type"] = item.get("type", "")

    return records


# ---------------------------------------------------------------------------
# Crossref
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
        timeout=30,
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
        year = date_parts[0][0] if date_parts and date_parts[0] else ""

        doi = item.get("DOI", "")
        doi_url = f"https://doi.org/{doi}" if doi else ""

        full_text_url = ""
        for link in item.get("link", []):
            content_type = (link.get("content-type") or "").lower()
            if "pdf" in content_type and link.get("URL"):
                full_text_url = link["URL"]
                break

        records.append(
            make_record(
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
        )

        records[-1]["type"] = item.get("type", "")

    return records


# ---------------------------------------------------------------------------
# PubMed
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
        timeout=30,
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
        timeout=30,
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
# Europe PMC
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
        timeout=30,
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
        link_items = (
            item.get("fullTextUrlList", {})
            .get("fullTextUrl", [])
        )

        if isinstance(link_items, dict):
            link_items = [link_items]

        for link in link_items:
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
# arXiv
# ---------------------------------------------------------------------------

@st.cache_data(ttl=3600, show_spinner=False)
def search_arxiv(query, start_year, end_year, limit):
    # Request extra records because results are filtered by year afterward.
    response = requests.get(
        "https://export.arxiv.org/api/query",
        params={
            "search_query": f"all:{query}",
            "start": 0,
            "max_results": min(limit * 3, 100),
            "sortBy": "relevance",
        },
        headers=HEADERS,
        timeout=40,
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
                if not (int(start_year) <= int(year) <= int(end_year)):
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
# DOAJ
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
        timeout=30,
    )
    response.raise_for_status()

    records = []

    for result in response.json().get("results", []):
        bib = result.get("bibjson", {})
        year = bib.get("year", "")

        if year:
            try:
                if not (int(start_year) <= int(year) <= int(end_year)):
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
        doi = bib.get("identifier", [])

        doi_value = ""
        for identifier in doi:
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
# CORE
# ---------------------------------------------------------------------------

def search_core(query, start_year, end_year, limit, api_key):
    if not api_key:
        raise ValueError(
            "CORE needs an API key. Add CORE_API_KEY to your secrets "
            "or environment variables."
        )

    response = requests.post(
        "https://api.core.ac.uk/v3/search/works",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"q": query, "limit": limit},
        timeout=40,
    )
    response.raise_for_status()

    records = []

    for item in response.json().get("results", []):
        year = item.get("year")

        if year:
            try:
                if not (int(start_year) <= int(year) <= int(end_year)):
                    continue
            except (TypeError, ValueError):
                pass

        authors = []
        for author in item.get("authors", []):
            if isinstance(author, dict):
                authors.append(author.get("name", ""))
            elif isinstance(author, str):
                authors.append(author)

        full_text_url = item.get("downloadUrl", "")
        if not full_text_url:
            urls = item.get("sourceFulltextUrls", [])
            if urls:
                full_text_url = urls[0]

        doi = item.get("doi", "")
        if doi and not doi.startswith("http"):
            doi = f"https://doi.org/{doi}"

        source = item.get("source") or {}
        record_url = doi or source.get("url", "")

        records.append(
            make_record(
                title=item.get("title"),
                authors=", ".join(name for name in authors if name),
                year=year,
                abstract=item.get("abstract", ""),
                url=record_url,
                full_text_url=full_text_url,
                database="CORE",
                doi=doi,
            )
        )

    return records


# ---------------------------------------------------------------------------
# OpenAIRE
# ---------------------------------------------------------------------------

def local_name(tag):
    """Remove an XML namespace from a tag name."""
    return tag.rsplit("}", 1)[-1].lower()


def descendant_text(element, wanted_names):
    """Find text in a descendant XML element by its local tag name."""
    wanted_names = {name.lower() for name in wanted_names}

    for child in element.iter():
        if local_name(child.tag) in wanted_names and child.text:
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
        timeout=40,
    )
    response.raise_for_status()

    root = ET.fromstring(response.text)
    records = []

    # OpenAIRE's XML structure may include nested result elements.
    for element in root.iter():
        if local_name(element.tag) != "result":
            continue

        title = descendant_text(element, {"title"})
        if not title:
            continue

        authors = []
        for child in element.iter():
            if local_name(child.tag) in {"creator", "author"} and child.text:
                name = child.text.strip()
                if name and name not in authors:
                    authors.append(name)

        year = descendant_text(
            element,
            {"dateofacceptance", "publicationdate", "date"},
        )
        year_match = re.search(r"\b(19|20)\d{2}\b", year or "")
        year = year_match.group(0) if year_match else ""

        # OpenAIRE may list repository or full-text-related URLs.
        # We label these as listed links, not as verified downloadable files.
        listed_url = ""
        full_text_url = ""

        for child in element.iter():
            tag = local_name(child.tag)
            if tag in {"webresource", "url"} and child.text:
                candidate = child.text.strip()
                if candidate.startswith("http"):
                    listed_url = listed_url or candidate

                    if tag == "webresource":
                        full_text_url = candidate

        doi = ""
        for child in element.iter():
            if local_name(child.tag) == "pid" and child.text:
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
                abstract=descendant_text(element, {"description"}),
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
# AI literature-review outline
# ---------------------------------------------------------------------------

def generate_outline(question, records):
    api_key = get_secret("OPENAI_API_KEY")

    if not api_key:
        raise ValueError(
            "To use AI synthesis, add OPENAI_API_KEY to "
            ".streamlit/secrets.toml or your environment variables."
        )

    source_text = []

    for index, record in enumerate(records[:30], start=1):
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

Create a literature-review outline using only the source details below.

Requirements:
- Group relevant sources into themes.
- Cite source IDs such as [S1] next to claims.
- Do not invent findings, methods, or citation details.
- Clearly say when the provided abstract does not contain enough information.
- Treat a listed full-text link as unverified; do not claim you read the full text.
- This is an outline for a human researcher to verify, not a finished paper.

Sources:
{chr(10).join(source_text)}
"""

    response = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-4o-mini",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a cautious research assistant. "
                        "Use only the supplied source information and "
                        "never invent evidence or citations."
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
# User interface
# ---------------------------------------------------------------------------

st.title("📚 Research Assistant")
st.write(
    "Search scholarly sources, review paper records and links, "
    "and create a source-linked literature-review outline."
)

st.info(
    "A 'Full-text link listed' label means a provider listed a possible "
    "full-text link. The app does not verify that the file opens or that "
    "you have permission to access it."
)

with st.form("research_search"):
    question = st.text_input(
        "Research topic or question",
        placeholder="For example: How does urban green space affect mental health?",
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
        options=ALL_SOURCES,
        default=DEFAULT_SOURCES,
        help="CORE requires a CORE API key. Add it to Streamlit secrets.",
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
            "CORE": lambda: search_core(
                question,
                int(start_year),
                int(end_year),
                int(limit),
                get_secret("CORE_API_KEY"),
            ),
            "OpenAIRE": lambda: search_openaire(
                question, int(start_year), int(end_year), int(limit)
            ),
        }

        all_records = []

        for source in selected_sources:
            try:
                with st.spinner(f"Searching {source}..."):
                    all_records.extend(search_functions[source]())
            except requests.RequestException as exc:
                st.warning(f"{source} search failed: {exc}")
            except Exception as exc:
                st.warning(f"{source} could not be searched: {exc}")

        records = combine_and_deduplicate(all_records)

        st.session_state["records"] = records
        st.session_state["question"] = question


# Display search results saved in this session.
if "records" in st.session_state:
    records = st.session_state["records"]

    st.subheader(f"Results: {len(records)} unique records")

    if not records:
        st.info(
            "No results were returned. Try different wording, a wider date "
            "range, or different sources."
        )
    else:
        table_rows = []

        for record in records:
            table_rows.append(
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
            pd.DataFrame(table_rows),
            use_container_width=True,
            hide_index=True,
            column_config={
                "Record link": st.column_config.LinkColumn("Record link"),
                "Full-text link": st.column_config.LinkColumn("Full-text link"),
            },
        )

        csv_data = pd.DataFrame(records).to_csv(index=False).encode("utf-8")

        st.download_button(
            label="Download results as CSV",
            data=csv_data,
            file_name="research_sources.csv",
            mime="text/csv",
        )

        with st.expander("Review abstracts and source details"):
            for index, record in enumerate(records, start=1):
                st.markdown(f"### [{index}] {record.get('title', 'Untitled')}")

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
                        f"[Open listed full-text link]"
                        f"({record['full_text_url']})"
                    )

                st.write(
                    record.get("abstract")
                    or "No abstract was provided by this source."
                )
                st.divider()

        if st.button("Generate literature-review outline"):
            try:
                with st.spinner("Creating a draft outline..."):
                    outline = generate_outline(
                        st.session_state["question"],
                        records,
                    )

                st.subheader("Draft literature-review outline")
                st.markdown(outline)
                st.warning(
                    "Verify every claim and citation against the original "
                    "paper before using it in academic work."
                )

            except requests.RequestException as exc:
                st.error(f"The AI service request failed: {exc}")
            except Exception as exc:
                st.error(str(exc))
