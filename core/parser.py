"""Data schema validation and normalization.

Turns messy candidate dicts (from LLM mapping or markdown heuristics) into
typed, deduplicated records that satisfy the user-defined schema. Tracks a
validity ratio so jobs can prove the >= 90% non-null acceptance bar.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from enum import Enum
from typing import Any, Iterable
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Placeholder addresses LLMs invent when the page shows no real contact
# (seen live: "verified@acme.io" on a job that never found the company).
_PLACEHOLDER_EMAIL_DOMAINS = frozenset({
    "example.com", "example.org", "example.net",
    "test.com", "domain.com", "company.com", "yourcompany.com",
    "yourdomain.com", "sample.com", "placeholder.com", "localhost",
})
_PLACEHOLDER_EMAIL_LOCALS = frozenset({
    "verified", "test", "example", "sample", "placeholder", "yourname",
    "firstname", "lastname", "username", "user", "email", "name",
    "johndoe", "janedoe", "test1",
})
URL_RE = re.compile(r"https?://[^\s)\]>]+")
URL_LIKE_RE = re.compile(r"^(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/[^\s]*)?$", re.I)
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^)]+)\)")

_DATE_FORMATS = ("%Y-%m-%d", "%d %b %Y", "%b %d, %Y", "%B %d, %Y", "%d/%m/%Y",
                 "%m/%d/%Y", "%Y", "%d-%m-%Y")


class FieldType(str, Enum):
    STRING = "string"
    EMAIL = "email"
    URL = "url"
    NUMBER = "number"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    DATE = "date"
    ARRAY = "array"


class SchemaField(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    type: FieldType = FieldType.STRING
    required: bool = False
    description: str = ""

    @field_validator("name")
    @classmethod
    def _normalize_name(cls, value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9]+", "_", value.strip()).strip("_").lower()
        if not cleaned:
            raise ValueError("field name must contain at least one alphanumeric character")
        return cleaned


class ParseStats(BaseModel):
    pages: int = 0
    candidates: int = 0
    valid: int = 0
    invalid: int = 0
    duplicates: int = 0

    @property
    def validity_ratio(self) -> float:
        denom = self.valid + self.invalid
        return round(self.valid / denom, 4) if denom else 0.0


class ParsedRecord(BaseModel):
    values: dict[str, Any]
    source_url: str
    errors: list[str] = []


# --------------------------------------------------------------------------
# coercion helpers
# --------------------------------------------------------------------------

def _to_email(value: Any) -> str | None:
    match = EMAIL_RE.search(str(value))
    if not match:
        return None
    email = match.group(0).lower()
    local, _, domain = email.partition("@")
    # LLM/hallucination placeholders — never a real contact address.
    if domain in _PLACEHOLDER_EMAIL_DOMAINS or local in _PLACEHOLDER_EMAIL_LOCALS:
        return None
    return email


def _to_url(value: Any, source_text: str = "") -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = URL_RE.search(text)
    if match:
        return match.group(0).rstrip(".,;")
    # Prefer links that actually exist on the source page (anti-hallucination).
    for label, href in MARKDOWN_LINK_RE.findall(source_text):
        if text.lower() in label.lower() or label.lower() in text.lower():
            return href.rstrip(".,;")
    if URL_LIKE_RE.match(text):
        return f"https://{text}"
    return None


def _to_number(value: Any, integer: bool = False) -> float | int | None:
    if isinstance(value, bool):
        return int(value) if integer else float(value)
    if isinstance(value, (int, float)):
        return int(value) if integer else float(value)
    text = str(value)
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", text)
    if not match:
        return None
    num = float(match.group(0).replace(",", ""))
    return int(round(num)) if integer else num


def _to_bool(value: Any) -> bool | None:
    text = str(value).strip().lower()
    if text in {"true", "yes", "y", "1", "active", "verified"}:
        return True
    if text in {"false", "no", "n", "0", "inactive", "unverified"}:
        return False
    return None


def _to_date(value: Any) -> str | None:
    text = str(value).strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    match = re.search(r"(20\d{2}|19\d{2})", text)
    return match.group(1) if match else None


def _to_array(value: Any) -> list[str] | None:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed]
        except json.JSONDecodeError:
            pass
    parts = [p.strip() for p in re.split(r"[,;|]", text) if p.strip()]
    return parts or None


def coerce_value(field: SchemaField, value: Any, source_text: str = "") -> Any:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    ftype = field.type
    try:
        if ftype is FieldType.EMAIL:
            return _to_email(value)
        if ftype is FieldType.URL:
            return _to_url(value, source_text)
        if ftype is FieldType.NUMBER:
            return _to_number(value, integer=False)
        if ftype is FieldType.INTEGER:
            return _to_number(value, integer=True)
        if ftype is FieldType.BOOLEAN:
            return _to_bool(value)
        if ftype is FieldType.DATE:
            return _to_date(value)
        if ftype is FieldType.ARRAY:
            return _to_array(value)
        text = str(value).strip()
        text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # [label](url) -> label
        return re.sub(r"\s+", " ", text).strip() or None
    except (ValueError, TypeError):
        return None


def norm_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


# --------------------------------------------------------------------------
# markdown heuristics
# --------------------------------------------------------------------------

_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}")
_KV_RES = [
    re.compile(r"^\s*(?:[-*]\s+)?\*\*([^*]{1,60})\*\*\s*[:：]\s*(.+)$", re.MULTILINE),
    re.compile(r"^\s*(?:[-*]\s+)?([A-Za-z][A-Za-z0-9 /&_+-]{1,40})\s*[:：]\s*(.+)$", re.MULTILINE),
]


def _split_markdown_table(markdown: str) -> list[dict[str, str]] | None:
    lines = markdown.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        if "|" in line and i + 1 < len(lines) and _TABLE_SEP_RE.match(lines[i + 1]) and "|" in lines[i + 1]:
            header_idx = i
            break
    if header_idx is None:
        return None
    def cells(line: str) -> list[str]:
        return [c.strip() for c in line.strip().strip("|").split("|")]
    headers = cells(lines[header_idx])
    rows: list[dict[str, str]] = []
    for line in lines[header_idx + 2:]:
        if "|" not in line:
            if rows:
                break
            continue
        values = cells(line)
        if len(values) < len(headers):
            values += [""] * (len(headers) - len(values))
        rows.append({h: v for h, v in zip(headers, values) if h})
    return rows or None


class RecordParser:
    """Validates candidate records against the user schema and dedupes them."""

    def __init__(self, fields: Iterable[SchemaField]):
        self.fields = list(fields)
        if not self.fields:
            raise ValueError("schema must contain at least one field")
        self.stats = ParseStats()
        self._seen: set[str] = set()

    @property
    def headers(self) -> list[str]:
        return [f.name for f in self.fields]

    # -- stage 1: raw candidates from markdown (heuristic path) --------------
    def heuristic_records(self, markdown: str, source_url: str, title: str = "") -> list[dict[str, Any]]:
        if is_error_page_title(title):
            return []
        candidates: list[dict[str, Any]] = []

        table = _split_markdown_table(markdown)
        if table:
            header_map = self._map_headers(list(table[0].keys()))
            if len(header_map) >= max(1, len(self.fields) // 3):
                for row in table:
                    candidates.append({f.name: row.get(h, "")
                                       for f, h in ((f, header_map.get(f.name)) for f in self.fields)
                                       if h and row.get(h)})

        kv: dict[str, str] = {}
        for pattern in _KV_RES:
            for match in pattern.finditer(markdown):
                key, value = match.group(1).strip(), match.group(2).strip()
                if len(value) > 400:
                    continue
                kv[norm_key(key)] = value
        if kv:
            single: dict[str, Any] = {}
            for field in self.fields:
                value = self._match_kv(kv, field)
                if value is None:
                    if field.type is FieldType.EMAIL:
                        found = EMAIL_RE.search(markdown)
                        value = found.group(0) if found else None
                    elif field.type is FieldType.URL:
                        value = source_url
                    elif _is_name_field(field):
                        value = _best_name(title, markdown, source_url)
                single[field.name] = value
            if any(v for v in single.values()):
                candidates.append(single)

        if not candidates:
            bare: dict[str, Any] = {}
            for field in self.fields:
                if field.type is FieldType.EMAIL:
                    found = EMAIL_RE.search(markdown)
                    bare[field.name] = found.group(0) if found else None
                elif field.type is FieldType.URL:
                    bare[field.name] = source_url
                elif _is_name_field(field):
                    bare[field.name] = _best_name(title, markdown, source_url)
            if any(v for v in bare.values()):
                candidates.append(bare)
        return candidates

    def _map_headers(self, headers: list[str]) -> dict[str, str]:
        mapping: dict[str, str] = {}
        norm_headers = {h: norm_key(h) for h in headers}
        for field in self.fields:
            target = norm_key(field.name)
            for original, normalized in norm_headers.items():
                if not normalized:
                    continue
                if target == normalized or target in normalized or normalized in target:
                    mapping[field.name] = original
                    break
        return mapping

    @staticmethod
    def _match_kv(kv: dict[str, str], field: SchemaField) -> str | None:
        target = norm_key(field.name)
        words = [w for w in re.split(r"[_\s]+", field.name) if len(w) > 2]
        for key, value in kv.items():
            if target and (target in key or key in target):
                return value
        for key, value in kv.items():
            if words and all(w in key for w in words):
                return value
        return None

    # -- stage 2: validate + normalize + dedupe --------------------------------
    def validate(self, candidate: dict[str, Any], source_url: str,
                 source_text: str = "") -> ParsedRecord:
        values: dict[str, Any] = {}
        errors: list[str] = []
        for field in self.fields:
            raw = candidate.get(field.name)
            if raw is None:
                # case-insensitive / snake-insensitive fallback lookup
                for key, val in candidate.items():
                    if norm_key(key) == norm_key(field.name):
                        raw = val
                        break
            value = coerce_value(field, raw, source_text)
            if value is None:
                # cross-fill: some schemas ask for `website` where the page
                # only exposes the source URL — safe default, never invented.
                if field.type is FieldType.URL and norm_key(field.name) in {"url", "website", "homepage", "source"}:
                    if source_url in source_text or not source_text:
                        value = source_url
                if field.required:
                    errors.append(f"{field.name}: missing")
            values[field.name] = value
        self._normalize_name(values, errors, source_url)
        return ParsedRecord(values=values, source_url=source_url, errors=errors)

    def _normalize_name(self, values: dict[str, Any], errors: list[str],
                        source_url: str) -> None:
        """Replace a missing or non-org-shaped name with a derived one.

        Page <title>s are SEO taglines ("Best Mobile App & Website
        Development Company in India"), article headlines, or slogans —
        never the legal entity. When the extracted name fails
        _looks_like_org_name (or is absent), derive one from the record's
        own contact identity: the email domain first (skip freemail),
        then the website host (skip platforms like linkedin.com where the
        host tells us nothing), then the source URL host. Keeps the
        original only when no better source exists — a tagged name beats
        an empty required field.
        """
        field = next((f for f in self.fields if _is_name_field(f)), None)
        if field is None:
            return
        current = values.get(field.name)
        if current is not None and _looks_like_org_name(str(current)):
            return
        derived = self._derive_name(values, source_url)
        if derived:
            values[field.name] = derived
            missing = f"{field.name}: missing"
            if missing in errors:
                errors.remove(missing)

    def _derive_name(self, values: dict[str, Any], source_url: str) -> str | None:
        email = next((str(values.get(f.name) or "").strip().lower()
                      for f in self.fields if f.type is FieldType.EMAIL), "")
        if email and "@" in email:
            domain = email.rsplit("@", 1)[1]
            if not _is_freemail(domain):
                name = _name_from_host(domain)
                if name:
                    return name
        url = next((str(values.get(f.name) or "").strip()
                    for f in self.fields if f.type is FieldType.URL), "")
        for candidate in (url, source_url):
            if not candidate:
                continue
            try:
                host = (urlparse(candidate).hostname or "").lower()
            except ValueError:
                continue
            if _registrable_host(host) in _PLATFORM_HOSTS:
                continue
            name = _name_from_host(host)
            if name:
                return name
        return None

    def ingest(self, candidates: Iterable[dict[str, Any]], source_url: str,
               source_text: str = "") -> list[dict[str, Any]]:
        accepted: list[dict[str, Any]] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            self.stats.candidates += 1
            parsed = self.validate(candidate, source_url, source_text)
            if parsed.errors:
                self.stats.invalid += 1
                continue
            dedupe_key = self._record_key(parsed.values)
            if dedupe_key in self._seen:
                self.stats.duplicates += 1
                continue
            self._seen.add(dedupe_key)
            self.stats.valid += 1
            accepted.append(parsed.values)
        return accepted

    def mark_page(self) -> None:
        self.stats.pages += 1

    def _record_key(self, values: dict[str, Any]) -> str:
        # Same-org key first: an email at a company's own domain plus a
        # website on that domain identifies one entity — revisits across
        # pages (same mailbox, different titles/URLs) collapse. Directory
        # rows sharing a generic contact address but listing their OWN
        # websites (email domain != website host) fall through to the
        # full-row key, so distinct companies still all survive.
        org = self._org_key(values)
        if org:
            return org
        # Full-row key: only genuinely identical rows collapse. Keying on a
        # single field (email, or the cross-filled source URL) used to wipe
        # out whole batches — e.g. every table row of a directory page
        # sharing url=source_url, or several companies listing the same
        # generic contact email — capping yields far below the record target.
        parts = [str(values.get(f.name) or "").strip().lower()
                 for f in self.fields]
        return "|".join(parts) if any(parts) else "empty"

    def _org_key(self, values: dict[str, Any]) -> str | None:
        email = next((str(values.get(f.name) or "").strip().lower()
                      for f in self.fields if f.type is FieldType.EMAIL), "")
        url = next((str(values.get(f.name) or "").strip()
                    for f in self.fields if f.type is FieldType.URL), "")
        if not email or "@" not in email or not url:
            return None
        try:
            host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        except ValueError:
            return None
        if not host or "." not in host:
            return None
        domain = email.rsplit("@", 1)[1].lower()
        if domain == host or domain.endswith("." + host) or host.endswith("." + domain):
            return f"org|{domain}|{host}"
        return None

    # -- utility ---------------------------------------------------------------
    @staticmethod
    def extract_json_payloads(text: str) -> list[dict[str, Any]]:
        """Pull JSON objects/arrays out of fenced blocks or raw text."""
        out: list[dict[str, Any]] = []
        for block in re.findall(r"```(?:json)?\s*(.*?)```", text, re.DOTALL):
            out.extend(_parse_json_container(block))
        if not out:
            out.extend(_parse_json_container(text))
        return out


def _parse_json_container(text: str) -> list[dict[str, Any]]:
    text = text.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = min([i for i in (text.find("["), text.find("{")) if i >= 0], default=-1)
        if start < 0:
            return []
        try:
            parsed = json.loads(text[start:text.rfind("]") + 1 if text.find("[") >= 0
                                     else text.rfind("}") + 1])
        except json.JSONDecodeError:
            return []
    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    return []


def _is_name_field(field: SchemaField) -> bool:
    return norm_key(field.name) in {"name", "company", "companyname", "title", "startup",
                                    "brand", "product", "organization", "org"}


# Titles of interstitial / bot-challenge / error pages. Scraping these
# yields fake records (e.g. "REQUEST DENIED!" rows carrying the site's
# contact email) — the whole page is worthless, skip it entirely.
_ERROR_TITLE_RE = re.compile(
    r"request denied|access denied|attention required|just a moment|"
    r"checking your browser|are you a robot|verify you are human|captcha|"
    r"403 forbidden|404 not found|error \d{3}|service unavailable|"
    r"temporarily (?:unavailable|blocked|limited)|enable javascript|"
    r"ddos protection|blocked\b",
    re.IGNORECASE,
)


def is_error_page_title(title: str) -> bool:
    return bool(title) and bool(_ERROR_TITLE_RE.search(title))


# Titles that identify a page type, never an entity — used as company_name
# they produce junk like "Home" or "Get in touch" rows.
_GENERIC_TITLES = frozenset({
    "home", "homepage", "home page", "welcome", "index", "untitled",
    "get in touch", "contact", "contact us", "contact page",
    "about", "about us", "about us!", "login", "sign in", "log in",
    "search", "404", "404 not found", "not found", "error",
    "services", "our services", "blog", "careers", "careers home",
})


def _best_name(title: str, markdown: str, source_url: str) -> str | None:
    """Best entity-name guess: usable title, else first heading, else domain stem."""
    for candidate in (title.strip(), _first_heading(markdown) or ""):
        cleaned = re.sub(r"\s*[|\-–—·•]\s*$", "", candidate).strip()
        if cleaned and cleaned.lower().rstrip("!") not in _GENERIC_TITLES \
                and _looks_like_org_name(cleaned):
            return cleaned
    return _name_from_url(source_url)


def _name_from_url(url: str) -> str | None:
    try:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return None
    return _name_from_host(host)


def _name_from_host(host: str) -> str | None:
    host = (host or "").lower().removeprefix("www.")
    if not host or "." not in host:
        return None
    stem = host.split(".")[0]
    if not stem or stem.isdigit() or len(stem) < 3:
        return None
    return stem[0].upper() + stem[1:]


_FREE_MAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "live.com", "msn.com", "aol.com", "icloud.com", "me.com", "proton.me",
    "protonmail.com", "zoho.com", "gmx.com", "gmx.de", "yandex.com",
    "yandex.ru", "mail.com", "mail.ru", "rediffmail.com", "inbox.com",
})

# First labels of freemail hosts that use country subdomains
# (yahoo.co.in, gmail.com.br, …) where the registrable host differs.
_FREE_MAIL_STEMS = frozenset({
    "gmail", "googlemail", "yahoo", "hotmail", "outlook", "live", "msn",
    "aol", "icloud", "me", "proton", "protonmail", "zoho", "gmx", "yandex",
    "rediffmail", "mail", "inbox",
})


def _is_freemail(domain: str) -> bool:
    if not domain:
        return False
    return (domain in _FREE_MAIL_DOMAINS
            or _registrable_host(domain) in _FREE_MAIL_DOMAINS
            or domain.split(".")[0] in _FREE_MAIL_STEMS)

# Hosts that identify a platform, not the entity on the page.
_PLATFORM_HOSTS = frozenset({
    "linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com",
    "youtube.com", "github.com", "wikipedia.org", "medium.com", "substack.com",
})


def _registrable_host(host: str) -> str:
    parts = (host or "").lower().removeprefix("www.").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else ""


# Sentence-y / slogan shapes that never describe a legal entity.
_SLOGAN_RE = re.compile(
    r"^(?:best|leading|top|trusted|award[- ]winning|your|get|find|we are|"
    r"about|list of|search|discover|why|how|what)\b",
    re.IGNORECASE,
)
# …and truncated ones: a name that ENDS on a slogan adjective is the tail
# of a tagline ("India's Leading" from "India's Leading Software Company").
_SLOGAN_TAIL_RE = re.compile(
    r"\b(?:leading|trusted|award[- ]winning|premium|fastest|growing)\.?$",
    re.IGNORECASE,
)
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF☀-➿⬀-⯿️]")


def _looks_like_org_name(name: str) -> bool:
    """False for taglines, article headlines, and slogans used as names.

    Heuristics (tuned against real junk from the Nagpur run):
      - >6 words or >60 chars → descriptive sentence, not an org name
        ("Leading Digital Marketing & Software Company in Nagpur,India")
      - ends in '!' / contains '?' → marketing copy
      - leading emoji / non-Latin script → article titles ("📰 EarEase…",
        Korean blog headlines); real orgs in listings are Latin here
      - slogan openers ("Best ", "Top ", "List of ") — only when the name
        is 3+ words, so "Best Buy" survives but "Best Mobile App & …"
        does not
      - prose markers that never occur in org names
    """
    s = name.strip()
    if not s or len(s) > 60:
        return False
    if _EMOJI_RE.search(s):
        return False
    if s.endswith("!") or "?" in s:
        return False
    # Non-Latin scripts (CJK, Hangul, Arabic, Cyrillic…) appearing in the
    # name slot came from foreign article titles, not company records.
    if re.search(r"[Ѐ-ӿ؀-ۿ぀-ヿ一-鿿가-힯]", s):
        return False
    words = s.split()
    if len(words) > 6:
        return False
    if _SLOGAN_RE.match(s) and len(words) > 2:
        return False
    if _SLOGAN_TAIL_RE.search(s):
        return False
    # Prose markers: phrases that never occur in org names.
    low = f" {s.lower()} "
    for marker in (" for all ", " your digital ", " needs ", " we help ",
                   " the emerging ", " of india", ",india", " in nagpur"):
        if marker in low:
            return False
    return True


def _first_heading(markdown: str) -> str | None:
    match = re.search(r"^#{1,2}\s+(.+)$", markdown, re.MULTILINE)
    if match:
        return match.group(1).strip()
    return None
