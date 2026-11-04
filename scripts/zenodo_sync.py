#!/usr/bin/env python3
"""Apply current-schema metadata to Zenodo records after a release.

Engine for the zenodo-schema-sync composite GitHub Action. Standard library
only, so the action has no install step.
"""

from __future__ import annotations

import argparse
import copy
import difflib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable

VERSION = "1.0.0"
USER_AGENT = (
    f"zenodo-schema-sync/{VERSION} "
    "(+https://github.com/tiadams/zenodo-schema-sync)"
)
RDM_ACCEPT = "application/vnd.inveniordm.v1+json"
DEFAULT_BASE_URL = "https://zenodo.org"
DEFAULT_CONFIG = ".github/zenodo-sync.json"
DEFAULT_PUBLISH_TIMEOUT = 300
DEFAULT_HTTP_TIMEOUT = 30

IGNORED_RESPONSE_ERRORS = {"files.enabled"}

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_CONFIG = 2
EXIT_API = 3
EXIT_TIMEOUT = 4

TOP_LEVEL_KEYS = {
    "$schema",
    "record",
    "concept",
    "repository",
    "version",
    "metadata",
    "custom",
    "custom_fields",
    "access",
}

METADATA_KEYS = {
    "resource_type",
    "creators",
    "title",
    "additional_titles",
    "publisher",
    "publication_date",
    "subjects",
    "contributors",
    "dates",
    "languages",
    "identifiers",
    "related_identifiers",
    "sizes",
    "formats",
    "version",
    "rights",
    "copyright",
    "description",
    "additional_descriptions",
    "locations",
    "funding",
    "references",
}

REQUIRED_FOR_CREATE = (
    "title",
    "creators",
    "resource_type",
    "publication_date",
    "publisher",
)

DOMAIN_CUSTOM_NAMESPACES = {
    "dwc",
    "gbif-dwc",
    "gbif",
    "mixs",
    "ac",
    "openbiodiv",
    "dc",
}

DOMAIN_CUSTOM_FIELDS = {
    "dwc": {
        "basisOfRecord",
        "catalogNumber",
        "class",
        "collectionCode",
        "country",
        "county",
        "dateIdentified",
        "decimalLatitude",
        "decimalLongitude",
        "eventDate",
        "family",
        "genus",
        "identifiedBy",
        "individualCount",
        "institutionCode",
        "kingdom",
        "lifeStage",
        "locality",
        "materialSampleID",
        "namePublishedInID",
        "namePublishedInYear",
        "order",
        "otherCatalogNumbers",
        "phylum",
        "preparations",
        "recordedBy",
        "scientificName",
        "scientificNameAuthorship",
        "scientificNameID",
        "sex",
        "specificEpithet",
        "stateProvince",
        "taxonID",
        "taxonRank",
        "taxonomicStatus",
        "typeStatus",
        "verbatimElevation",
        "verbatimEventDate",
        "sampleSizeValue",
        "sampleSizeUnit",
        "associatedSequences",
        "eventID",
        "samplingProtocol",
        "locationID",
        "habitat",
        "countryCode",
        "samplingEffort",
        "geodeticDatum",
        "occurrenceID",
        "occurrenceStatus",
        "organismQuantity",
        "organismQuantityType",
        "parentEventID",
        "datasetID",
    },
    "gbif-dwc": {"identifiedByID", "recordedByID"},
    "gbif": {
        "ampliconSize",
        "DNA_sequence",
        "pcr_primer_forward",
        "pcr_primer_reverse",
        "pcr_primer_name_forward",
        "pcr_primer_name_reverse",
    },
    "mixs": {
        "0000090",
        "0000044",
        "0000045",
        "0000050",
        "0000049",
        "0000058",
        "0000012",
        "0000013",
        "0000014",
    },
    "ac": {
        "associatedSpecimenReference",
        "captureDevice",
        "physicalSetting",
        "resourceCreationTechnique",
        "subjectOrientation",
        "subjectPart",
        "digitizationDate",
    },
    "openbiodiv": {"TaxonomicConceptLabel"},
    "dc": {"creator", "rightsHolder"},
    "obo": {"RO_0002453"},
}

CUSTOM_FIELD_NAMESPACES = DOMAIN_CUSTOM_NAMESPACES | {
    "obo",
    "legacy",
    "code",
    "journal",
    "meeting",
    "imprint",
    "thesis",
}

NESTED_CUSTOM_FIELDS = {
    "journal:journal": {"title", "issue", "volume", "pages", "issn"},
    "meeting:meeting": {
        "acronym",
        "dates",
        "place",
        "session_part",
        "session",
        "title",
        "url",
        "identifiers",
    },
    "imprint:imprint": {"title", "isbn", "pages", "place", "edition"},
    "thesis:thesis": {
        "university",
        "department",
        "type",
        "date_submitted",
        "date_defended",
    },
}

PERSON_TYPES = {"personal", "organizational"}

PERSON_IDENTIFIER_SCHEMES = {"orcid", "isni", "gnd", "ror"}

IDENTIFIER_SCHEMES = {
    "ark",
    "arxiv",
    "ads",
    "crossreffunderid",
    "cstr",
    "doi",
    "ean13",
    "eissn",
    "grid",
    "handle",
    "igsn",
    "isbn",
    "isni",
    "issn",
    "istc",
    "lissn",
    "lsid",
    "pmid",
    "purl",
    "rrid",
    "upc",
    "url",
    "urn",
    "wikidata",
    "w3id",
    "other",
}

LOCATION_IDENTIFIER_SCHEMES = {"geonames", "wikidata"}

LOCATION_GEOMETRY_TYPES = {"Point", "Polygon"}

EDTF_RE = re.compile(
    r"^(?:"
    r"(?:\.\.|[0-9]{4}(?:-[0-9]{2}(?:-[0-9]{2})?)?"
    r"(?:[T ][0-9]{2}(?::[0-9]{2}(?::[0-9]{2})?)?(?:Z|[+-][0-9]{2}:?[0-9]{2})?)?)"
    r"(?:/(?:\.\.|[0-9]{4}(?:-[0-9]{2}(?:-[0-9]{2})?)?"
    r"(?:[T ][0-9]{2}(?::[0-9]{2}(?::[0-9]{2})?)?(?:Z|[+-][0-9]{2}:?[0-9]{2})?)?))?"
    r")$"
)

VOCAB_ID_FIELDS = {
    "resource_type": "resource_type",
    "languages": "language",
    "rights": "license",
    "dates": "date_type",
    "additional_titles": "title_type",
    "additional_descriptions": "description_type",
}


class SyncError(Exception):
    exit_code = EXIT_API


class ConfigError(SyncError):
    exit_code = EXIT_CONFIG


class DuplicateError(SyncError):
    exit_code = EXIT_DRIFT


class MissingDeposit(SyncError):
    exit_code = EXIT_DRIFT


class ApiError(SyncError):
    exit_code = EXIT_API

    def __init__(
        self,
        message: str,
        status: int | None = None,
        payload: Any = None,
    ):
        super().__init__(message)
        self.status = status
        self.payload = payload


class PublishTimeout(SyncError):
    exit_code = EXIT_TIMEOUT


class Redactor:
    def __init__(self, secrets: Iterable[str] = ()):
        self._secrets: list[str] = []
        for secret in secrets:
            self.add(secret)

    def add(self, secret: str | None) -> None:
        if secret and secret not in self._secrets:
            self._secrets.append(secret)

    def __call__(self, text: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, "***")
        return text


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str

    def json(self) -> Any:
        if not self.body:
            return None
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(
                f"Response from {self.url} was not valid JSON: {exc}"
            ) from exc


class HttpClient:
    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        timeout: int = DEFAULT_HTTP_TIMEOUT,
        redactor: Redactor | None = None,
        verbose: bool = False,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.redactor = redactor or Redactor([token])
        self.verbose = verbose

    def get(
        self,
        path: str,
        accept: str = RDM_ACCEPT,
        retries: int = 2,
        params: dict[str, Any] | None = None,
    ) -> Response:
        if params:
            query = urllib.parse.urlencode(params)
            separator = "&" if "?" in path else "?"
            path = f"{path}{separator}{query}"
        return self.request("GET", path, accept=accept, retries=retries)

    def post(self, path: str, body: Any = None) -> Response:
        return self.request("POST", path, body=body)

    def put(self, path: str, body: Any) -> Response:
        return self.request("PUT", path, body=body)

    def delete(self, path: str) -> Response:
        return self.request("DELETE", path)

    def put_bytes(self, path: str, data: bytes, content_type: str) -> Response:
        return self._send(
            "PUT",
            self._url(path),
            data,
            {
                "Accept": RDM_ACCEPT,
                "Content-Type": content_type,
                "User-Agent": USER_AGENT,
                **self._auth_header(),
            },
            retries=0,
        )

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        accept: str = RDM_ACCEPT,
        retries: int = 0,
    ) -> Response:
        data = None
        headers = {
            "Accept": accept,
            "User-Agent": USER_AGENT,
            **self._auth_header(),
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        response = self._send(
            method, self._url(path), data, headers, retries=retries
        )
        if method in {"POST", "PUT"}:
            self.check_response_errors(method, response.url, response)
        return response

    def download(
        self,
        url: str,
        destination: str,
        headers: dict[str, str] | None = None,
        accept: str = "application/octet-stream",
    ) -> None:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": accept,
                "User-Agent": USER_AGENT,
                **(headers or {}),
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=max(self.timeout, 60)
            ) as response:
                with open(destination, "wb") as handle:
                    shutil.copyfileobj(response, handle)
        except urllib.error.HTTPError as exc:
            body = exc.read()
            raise self._http_error("GET", url, exc.code, body) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ApiError(self.redactor(f"GET {url} failed: {reason}")) from exc

    def _url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    def _auth_header(self) -> dict[str, str]:
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return {}

    def _send(
        self,
        method: str,
        url: str,
        data: bytes | None,
        headers: dict[str, str],
        retries: int,
    ) -> Response:
        attempts = max(0, retries) + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            request = urllib.request.Request(
                url, data=data, headers=headers, method=method
            )
            if self.verbose:
                print(f"::debug::{method} {self.redactor(url)}", file=sys.stderr)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return Response(
                        status=response.getcode(),
                        headers=dict(response.headers.items()),
                        body=response.read(),
                        url=response.geturl(),
                    )
            except urllib.error.HTTPError as exc:
                body = exc.read()
                if (
                    method in {"GET", "HEAD"}
                    and exc.code in {429, 500, 502, 503, 504}
                    and attempt + 1 < attempts
                ):
                    last_error = exc
                    self._sleep(attempt, exc.headers.get("Retry-After"))
                    continue
                raise self._http_error(method, url, exc.code, body) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if method in {"GET", "HEAD"} and attempt + 1 < attempts:
                    self._sleep(attempt, None)
                    continue
                reason = getattr(exc, "reason", exc)
                raise ApiError(
                    self.redactor(f"{method} {url} failed: {reason}")
                ) from exc
        raise ApiError(self.redactor(f"{method} {url} failed: {last_error}"))

    def _sleep(self, attempt: int, retry_after: str | None) -> None:
        delay = min(2**attempt, 10)
        if retry_after:
            try:
                delay = min(float(retry_after), 30)
            except ValueError:
                pass
        time.sleep(delay)

    def _render_errors(self, errors: list[Any]) -> list[str]:
        details: list[str] = []
        for error in errors:
            if not isinstance(error, dict):
                details.append(str(error))
                continue
            path = error.get("field") or error.get("name") or ""
            if path in IGNORED_RESPONSE_ERRORS:
                continue
            messages = error.get("messages") or error.get("message") or []
            if isinstance(messages, str):
                messages = [messages]
            rendered = "; ".join(str(item) for item in messages)
            details.append(f"{path}: {rendered}" if path else rendered)
        return details

    def check_response_errors(
        self, method: str, url: str, response: Response
    ) -> None:
        if not 200 <= response.status < 300 or not response.body:
            return
        try:
            parsed = response.json()
        except ApiError:
            return
        errors = parsed.get("errors") if isinstance(parsed, dict) else None
        if not isinstance(errors, list) or not errors:
            return
        details = self._render_errors(errors)
        if not details:
            return
        location = urllib.parse.urlsplit(url).path or url
        raise ApiError(
            f"{method} {location} returned HTTP {response.status} with "
            f"errors: {' | '.join(details)}",
            status=response.status,
            payload=parsed,
        )

    def _http_error(
        self, method: str, url: str, status: int, body: bytes
    ) -> ApiError:
        text = self.redactor(body.decode("utf-8", "replace"))
        details: list[str] = []
        parsed: Any = None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            pass
        if isinstance(parsed, dict):
            message = parsed.get("message")
            if message:
                details.append(str(message))
            errors = parsed.get("errors")
            if isinstance(errors, list):
                details.extend(self._render_errors(errors))
        if not details:
            details.append(text.strip()[:800] or "no response body")
        location = urllib.parse.urlsplit(url).path or url
        return ApiError(
            f"{method} {location} returned HTTP {status}: {' | '.join(details)}",
            status=status,
        )


@dataclass
class VocabResult:
    supported: bool
    exists: bool
    suggestion: str | None = None


class Vocabularies:
    ITEM_PATHS = {
        "resource_type": "/api/vocabularies/resourcetypes/{id}",
        "relation_type": "/api/vocabularies/relationtypes/{id}",
        "license": "/api/vocabularies/licenses/{id}",
        "language": "/api/vocabularies/languages/{id}",
        "creator_role": "/api/vocabularies/creatorsroles/{id}",
        "contributor_role": "/api/vocabularies/contributorsroles/{id}",
        "date_type": "/api/vocabularies/datetypes/{id}",
        "description_type": "/api/vocabularies/descriptiontypes/{id}",
        "title_type": "/api/vocabularies/titletypes/{id}",
        "code:programmingLanguage": (
            "/api/vocabularies/code%3AprogrammingLanguages/{id}"
        ),
        "code:developmentStatus": "/api/vocabularies/code%3AdevelopmentStatus/{id}",
        "funder": "/api/funders/{id}",
        "award": "/api/awards/{id}",
        "affiliation": "/api/affiliations/{id}",
        "community": "/api/communities/{id}",
    }
    LIST_PATHS = {
        "resource_type": "/api/vocabularies/resourcetypes",
        "relation_type": "/api/vocabularies/relationtypes",
        "license": "/api/vocabularies/licenses",
        "language": "/api/vocabularies/languages",
        "creator_role": "/api/vocabularies/creatorsroles",
        "contributor_role": "/api/vocabularies/contributorsroles",
        "date_type": "/api/vocabularies/datetypes",
        "description_type": "/api/vocabularies/descriptiontypes",
        "title_type": "/api/vocabularies/titletypes",
        "code:programmingLanguage": "/api/vocabularies/code%3AprogrammingLanguages",
        "code:developmentStatus": "/api/vocabularies/code%3AdevelopmentStatus",
        "funder": "/api/funders",
        "award": "/api/awards",
    }
    UNSUPPORTED: set[str] = set()

    def __init__(self, client: HttpClient):
        self.client = client
        self._results: dict[tuple[str, str], VocabResult] = {}
        self._ids: dict[str, list[str] | None] = {}

    def check(self, kind: str, value: str) -> VocabResult:
        key = (kind, value)
        if key in self._results:
            return self._results[key]
        template = self.ITEM_PATHS.get(kind)
        if kind in self.UNSUPPORTED or not template:
            result = VocabResult(supported=False, exists=True)
            self._results[key] = result
            return result
        path = template.format(id=urllib.parse.quote(str(value), safe=""))
        try:
            self.client.get(path, retries=2)
            result = VocabResult(supported=True, exists=True)
        except ApiError as exc:
            if exc.status != 404:
                raise
            result = VocabResult(
                supported=True,
                exists=False,
                suggestion=self._suggest(kind, str(value)),
            )
        self._results[key] = result
        return result

    def _suggest(self, kind: str, value: str) -> str | None:
        ids = self._list_ids(kind)
        if not ids:
            return None
        matches = difflib.get_close_matches(value, ids, n=3, cutoff=0.6)
        if not matches:
            matches = [item for item in ids if value.lower() in item.lower()][:3]
        return ", ".join(matches) if matches else None

    def _list_ids(self, kind: str) -> list[str] | None:
        if kind in self._ids:
            return self._ids[kind]
        path = self.LIST_PATHS.get(kind)
        if not path:
            self._ids[kind] = None
            return None
        ids: list[str] = []
        try:
            try:
                response = self.client.get(path, retries=0, params={"size": 100})
            except ApiError as exc:
                if exc.status != 400:
                    raise
                response = self.client.get(path, retries=0, params={"size": 25})
            hits = ((response.json() or {}).get("hits") or {}).get("hits") or []
            ids = [hit["id"] for hit in hits if isinstance(hit.get("id"), str)]
        except ApiError:
            self._ids[kind] = None
            return None
        self._ids[kind] = ids or None
        return self._ids[kind]


def _json_path(path: str, key: Any) -> str:
    if isinstance(key, int):
        return f"{path}[{key}]"
    return f"{path}.{key}" if path else str(key)


def _expect_object(value: Any, path: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: expected a JSON object")
    return value


def _expect_list(value: Any, path: str) -> list:
    if not isinstance(value, list):
        raise ConfigError(f"{path}: expected an array")
    return value


def _expect_string(value: Any, path: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{path}: expected a string")
    if not allow_empty and not value.strip():
        raise ConfigError(f"{path}: must not be empty")
    return value


def _vocab_object(value: Any, path: str, extra: tuple[str, ...] = ()) -> dict:
    if isinstance(value, str):
        return {"id": _expect_string(value, path)}
    obj = _expect_object(value, path)
    unknown = set(obj) - {"id", *extra}
    if unknown:
        keys = ", ".join(sorted(unknown))
        raise ConfigError(f"{path}: unsupported key(s) {keys}")
    result = {"id": _expect_string(obj.get("id"), f"{path}.id")}
    for key in extra:
        if key in obj:
            result[key] = _expect_string(obj[key], f"{path}.{key}")
    return result


def _reject_unknown(obj: dict, allowed: set[str], path: str) -> None:
    unknown = set(obj) - allowed
    if unknown:
        keys = ", ".join(sorted(unknown))
        raise ConfigError(f"{path}: unsupported key(s) {keys}")


def _locale_text(value: Any, path: str) -> dict:
    if isinstance(value, str):
        return {"en": _expect_string(value, path)}
    obj = _expect_object(value, path)
    if len(obj) != 1:
        raise ConfigError(f"{path}: expected exactly one locale")
    for language, text in obj.items():
        if not isinstance(language, str) or not re.fullmatch(r"[a-z]{2}", language):
            raise ConfigError(f"{path}: '{language}' is not a valid locale code")
        _expect_string(text, f"{path}.{language}")
    return copy.deepcopy(obj)


def _identifier_item(
    value: Any, path: str, schemes: set[str], extra: set[str] | None = None
) -> dict:
    item = _expect_object(value, path)
    _reject_unknown(item, {"identifier", "scheme", *(extra or ())}, path)
    result = {"identifier": _expect_string(item.get("identifier"), f"{path}.identifier")}
    if "scheme" in item:
        scheme = _expect_string(item["scheme"], f"{path}.scheme")
        if scheme not in schemes:
            raise ConfigError(f"{path}.scheme: unsupported scheme '{scheme}'")
        result["scheme"] = scheme
    return result


def _person(value: Any, path: str) -> dict:
    person = _expect_object(value, path)
    _reject_unknown(
        person,
        {"type", "name", "given_name", "family_name", "identifiers"},
        path,
    )
    kind = _expect_string(person.get("type"), f"{path}.type")
    if kind not in PERSON_TYPES:
        raise ConfigError(
            f"{path}.type: expected 'personal' or 'organizational'"
        )
    result: dict[str, Any] = {"type": kind}
    given = person.get("given_name")
    family = person.get("family_name")
    if given is not None:
        given = _expect_string(given, f"{path}.given_name")
    if family is not None:
        family = _expect_string(family, f"{path}.family_name")
    if kind == "personal":
        if not family:
            name = person.get("name")
            if isinstance(name, str) and "," in name:
                family, given = (part.strip() for part in name.split(",", 1))
                if not family:
                    raise ConfigError(f"{path}: needs a family name")
            else:
                raise ConfigError(
                    f"{path}: personal names need 'family_name' "
                    "(or a 'Family, Given' name)"
                )
        result["family_name"] = family
        if given:
            result["given_name"] = given
        result["name"] = ", ".join(part for part in (family, given) if part)
    else:
        name = person.get("name")
        if isinstance(name, str) and given and family and not name.strip():
            name = None
        if name is None:
            name = " ".join(part for part in (given, family) if part)
        result["name"] = _expect_string(name, f"{path}.name")
    identifiers = person.get("identifiers")
    if identifiers is not None:
        result["identifiers"] = [
            _identifier_item(
                item, _json_path(f"{path}.identifiers", index), PERSON_IDENTIFIER_SCHEMES
            )
            for index, item in enumerate(_expect_list(identifiers, f"{path}.identifiers"))
        ]
    return result


def _affiliations(value: Any, path: str) -> list[dict]:
    result: list[dict] = []
    seen: set[str] = set()
    for index, entry in enumerate(_expect_list(value, path)):
        item_path = _json_path(path, index)
        if isinstance(entry, str):
            item = {"name": _expect_string(entry, item_path)}
        else:
            obj = _expect_object(entry, item_path)
            _reject_unknown(obj, {"id", "name"}, item_path)
            if obj.get("id") is not None:
                item = {
                    "id": _vocabulary_id(
                        obj["id"], f"{item_path}.id", "affiliation"
                    )
                }
            else:
                item = {"name": _expect_string(obj.get("name"), f"{item_path}.name")}
        marker = item.get("id") or item.get("name")
        if marker in seen:
            raise ConfigError(f"{item_path}: duplicate affiliation '{marker}'")
        seen.add(marker)
        result.append(item)
    return result


def _rights_item(value: Any, path: str) -> dict:
    if isinstance(value, str):
        return {"id": _expect_string(value, path)}
    item = _expect_object(value, path)
    _reject_unknown(item, {"id", "title", "description", "link"}, path)
    if item.get("id") is not None:
        if len(item) > 1:
            raise ConfigError(
                f"{path}: use either an id or free-text rights, not both"
            )
        return {"id": _expect_string(item["id"], f"{path}.id")}
    if item.get("title") is None:
        raise ConfigError(f"{path}: needs an id or a free-text title")
    result = {"title": _locale_text(item["title"], f"{path}.title")}
    if "description" in item:
        result["description"] = _locale_text(
            item["description"], f"{path}.description"
        )
    if "link" in item:
        link = _expect_string(item["link"], f"{path}.link")
        parsed = urllib.parse.urlsplit(link)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigError(f"{path}.link: expected an absolute http(s) URL")
        result["link"] = link
    return result


def _vocabulary_id(value: Any, path: str, kind: str) -> str:
    text = _expect_string(value, path)
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        raise ConfigError(
            f"{path}: use the Zenodo {kind} id, not a URL "
            f"(look it up under /api/{kind}s)"
        )
    return text


def _funder_item(value: Any, path: str) -> dict:
    if isinstance(value, str):
        return {"id": _vocabulary_id(value, path, "funder")}
    item = _expect_object(value, path)
    _reject_unknown(item, {"id", "name"}, path)
    if item.get("id") is not None:
        return {"id": _vocabulary_id(item["id"], f"{path}.id", "funder")}
    return {"name": _expect_string(item.get("name"), f"{path}.name")}


def _award_item(value: Any, path: str) -> dict:
    if isinstance(value, str):
        return {"id": _vocabulary_id(value, path, "award")}
    item = _expect_object(value, path)
    _reject_unknown(item, {"id", "number", "title", "identifiers"}, path)
    if item.get("id") is not None:
        return {"id": _vocabulary_id(item["id"], f"{path}.id", "award")}
    if item.get("number") is None and item.get("title") is None:
        raise ConfigError(f"{path}: needs an id, number, or title")
    result: dict[str, Any] = {}
    if "number" in item:
        result["number"] = _expect_string(item["number"], f"{path}.number")
    if "title" in item:
        result["title"] = _locale_text(item["title"], f"{path}.title")
    if "identifiers" in item:
        result["identifiers"] = [
            _identifier_item(
                entry, _json_path(f"{path}.identifiers", index), {"url", "doi"}
            )
            for index, entry in enumerate(
                _expect_list(item["identifiers"], f"{path}.identifiers")
            )
        ]
    return result


def _reference_item(value: Any, path: str) -> dict:
    if isinstance(value, str):
        return {"reference": _expect_string(value, path)}
    item = _expect_object(value, path)
    _reject_unknown(item, {"reference", "identifier", "scheme"}, path)
    result = {"reference": _expect_string(item.get("reference"), f"{path}.reference")}
    if "identifier" in item:
        result["identifier"] = _expect_string(
            item["identifier"], f"{path}.identifier"
        )
    if "scheme" in item:
        if "identifier" not in item:
            raise ConfigError(f"{path}.scheme: needs an identifier")
        scheme = _expect_string(item["scheme"], f"{path}.scheme")
        if scheme not in IDENTIFIER_SCHEMES:
            raise ConfigError(f"{path}.scheme: unsupported scheme '{scheme}'")
        result["scheme"] = scheme
    return result


def _subject_item(value: Any, path: str) -> dict:
    if isinstance(value, str):
        return {"subject": _expect_string(value, path)}
    item = _expect_object(value, path)
    _reject_unknown(item, {"subject"}, path)
    return {"subject": _expect_string(item.get("subject"), f"{path}.subject")}


def _geometry(value: Any, path: str) -> dict:
    item = _expect_object(value, path)
    _reject_unknown(item, {"type", "coordinates"}, path)
    kind = _expect_string(item.get("type"), f"{path}.type")
    if kind not in LOCATION_GEOMETRY_TYPES:
        raise ConfigError(
            f"{path}.type: supported geometry types are Point and Polygon; "
            "MultiPoint is rejected because Zenodo stores it but renders it "
            "as a broken record page"
        )
    coordinates = item.get("coordinates")

    def position(entry: Any, entry_path: str) -> list:
        values = _expect_list(entry, entry_path)
        if len(values) < 2 or not all(
            isinstance(number, (int, float)) and not isinstance(number, bool)
            for number in values
        ):
            raise ConfigError(
                f"{entry_path}: expected [longitude, latitude] numbers"
            )
        return list(values)

    if kind == "Point":
        result = position(coordinates, f"{path}.coordinates")
    else:
        rings = []
        for index, ring in enumerate(_expect_list(coordinates, f"{path}.coordinates")):
            ring_path = _json_path(f"{path}.coordinates", index)
            points = [
                position(entry, _json_path(ring_path, entry_index))
                for entry_index, entry in enumerate(_expect_list(ring, ring_path))
            ]
            if len(points) < 4 or points[0] != points[-1]:
                raise ConfigError(
                    f"{ring_path}: a polygon ring needs at least four positions "
                    "and must be closed"
                )
            rings.append(points)
        result = rings
    return {"type": kind, "coordinates": result}


def _location_feature(value: Any, path: str) -> dict:
    item = _expect_object(value, path)
    _reject_unknown(item, {"geometry", "place", "identifiers", "description"}, path)
    result: dict[str, Any] = {}
    if "geometry" in item:
        result["geometry"] = _geometry(item["geometry"], f"{path}.geometry")
    if "place" in item:
        result["place"] = _expect_string(item["place"], f"{path}.place")
    if "description" in item:
        result["description"] = _expect_string(
            item["description"], f"{path}.description"
        )
    if "identifiers" in item:
        result["identifiers"] = [
            _identifier_item(
                entry,
                _json_path(f"{path}.identifiers", index),
                LOCATION_IDENTIFIER_SCHEMES,
            )
            for index, entry in enumerate(
                _expect_list(item["identifiers"], f"{path}.identifiers")
            )
        ]
    if not result:
        raise ConfigError(
            f"{path}: needs at least one of geometry, place, identifiers, description"
        )
    return result


def _normalise_metadata(metadata: Any, path: str) -> dict:
    data = _expect_object(metadata, path)
    for key in data:
        if key not in METADATA_KEYS:
            raise ConfigError(f"{path}.{key}: unsupported metadata key")
    result: dict[str, Any] = {}
    for key, value in data.items():
        field_path = f"{path}.{key}"
        if value is None:
            result[key] = None
        elif key == "resource_type":
            result[key] = _vocab_object(value, field_path)
        elif key == "languages":
            result[key] = [
                _vocab_object(item, _json_path(field_path, index))
                for index, item in enumerate(_expect_list(value, field_path))
            ]
        elif key == "rights":
            result[key] = [
                _rights_item(item, _json_path(field_path, index))
                for index, item in enumerate(_expect_list(value, field_path))
            ]
        elif key in {"additional_titles", "additional_descriptions"}:
            label = "title" if key == "additional_titles" else "description"
            items = []
            for index, entry in enumerate(_expect_list(value, field_path)):
                item_path = _json_path(field_path, index)
                item = _expect_object(entry, item_path)
                _reject_unknown(item, {label, "type", "lang"}, item_path)
                text = _expect_string(item.get(label), f"{item_path}.{label}")
                if len(text) < 3:
                    raise ConfigError(
                        f"{item_path}.{label}: must be at least 3 characters"
                    )
                normalised = {label: text}
                normalised["type"] = _vocab_object(
                    item.get("type"), f"{item_path}.type"
                )
                if "lang" in item:
                    normalised["lang"] = _vocab_object(
                        item["lang"], f"{item_path}.lang"
                    )
                items.append(normalised)
            result[key] = items
        elif key == "dates":
            items = []
            for index, entry in enumerate(_expect_list(value, field_path)):
                item_path = _json_path(field_path, index)
                item = _expect_object(entry, item_path)
                _reject_unknown(item, {"date", "type", "description"}, item_path)
                date = _expect_string(item.get("date"), f"{item_path}.date")
                if not EDTF_RE.match(date):
                    raise ConfigError(
                        f"{item_path}.date: expected an EDTF date or interval"
                    )
                normalised = {"date": date}
                normalised["type"] = _vocab_object(
                    item.get("type"), f"{item_path}.type"
                )
                if "description" in item:
                    normalised["description"] = _expect_string(
                        item["description"], f"{item_path}.description"
                    )
                items.append(normalised)
            result[key] = items
        elif key == "subjects":
            result[key] = [
                _subject_item(item, _json_path(field_path, index))
                for index, item in enumerate(_expect_list(value, field_path))
            ]
        elif key in {"creators", "contributors"}:
            items = []
            for index, entry in enumerate(_expect_list(value, field_path)):
                item_path = _json_path(field_path, index)
                item = _expect_object(entry, item_path)
                _reject_unknown(
                    item, {"person_or_org", "role", "affiliations"}, item_path
                )
                normalised = {
                    "person_or_org": _person(
                        item.get("person_or_org"), f"{item_path}.person_or_org"
                    )
                }
                if "role" in item:
                    normalised["role"] = _vocab_object(
                        item["role"], f"{item_path}.role"
                    )
                elif key == "contributors":
                    raise ConfigError(f"{item_path}.role: required for contributors")
                if "affiliations" in item:
                    normalised["affiliations"] = _affiliations(
                        item["affiliations"], f"{item_path}.affiliations"
                    )
                items.append(normalised)
            if key == "creators" and not items:
                raise ConfigError(f"{field_path}: at least one creator is required")
            result[key] = items
        elif key == "identifiers":
            result[key] = [
                _identifier_item(
                    item, _json_path(field_path, index), IDENTIFIER_SCHEMES
                )
                for index, item in enumerate(_expect_list(value, field_path))
            ]
        elif key == "related_identifiers":
            items = []
            for index, entry in enumerate(_expect_list(value, field_path)):
                item_path = _json_path(field_path, index)
                item = _expect_object(entry, item_path)
                normalised = _identifier_item(
                    item,
                    item_path,
                    IDENTIFIER_SCHEMES,
                    extra={"relation_type", "resource_type"},
                )
                normalised["relation_type"] = _vocab_object(
                    item.get("relation_type"), f"{item_path}.relation_type"
                )
                if "resource_type" in item:
                    normalised["resource_type"] = _vocab_object(
                        item["resource_type"], f"{item_path}.resource_type"
                    )
                items.append(normalised)
            result[key] = items
        elif key == "funding":
            items = []
            for index, entry in enumerate(_expect_list(value, field_path)):
                item_path = _json_path(field_path, index)
                item = _expect_object(entry, item_path)
                _reject_unknown(item, {"funder", "award"}, item_path)
                normalised = {
                    "funder": _funder_item(item.get("funder"), f"{item_path}.funder")
                }
                if "award" in item:
                    normalised["award"] = _award_item(
                        item["award"], f"{item_path}.award"
                    )
                items.append(normalised)
            result[key] = items
        elif key == "references":
            result[key] = [
                _reference_item(item, _json_path(field_path, index))
                for index, item in enumerate(_expect_list(value, field_path))
            ]
        elif key == "locations":
            item = _expect_object(value, field_path)
            _reject_unknown(item, {"features"}, field_path)
            features = [
                _location_feature(entry, _json_path(f"{field_path}.features", index))
                for index, entry in enumerate(
                    _expect_list(item.get("features"), f"{field_path}.features")
                )
            ]
            if not features:
                raise ConfigError(f"{field_path}.features: at least one required")
            result[key] = {"features": features}
        elif key == "sizes" or key == "formats":
            result[key] = [
                _expect_string(item, _json_path(field_path, index))
                for index, item in enumerate(_expect_list(value, field_path))
            ]
        elif key == "version":
            version = _expect_string(value, field_path)
            if len(version) > 191:
                raise ConfigError(f"{field_path}: must be at most 191 characters")
            result[key] = version
        else:
            result[key] = _expect_string(value, field_path)
    return result


def _custom_scalar(value: Any, path: str) -> Any:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ConfigError(f"{path}: expected a string or number")
    return value


def _custom_list(value: Any, path: str) -> list:
    return [
        _custom_scalar(item, _json_path(path, index))
        for index, item in enumerate(_expect_list(value, path))
    ]


def _issn(value: str) -> bool:
    compact = value.replace("-", "").strip().upper()
    if len(compact) != 8 or not re.fullmatch(r"[0-9]{7}[0-9X]", compact):
        return False
    total = sum(int(digit) * weight for weight, digit in zip(range(8, 1, -1), compact))
    check = (11 - total % 11) % 11
    expected = "X" if check == 10 else str(check)
    return compact[-1] == expected


def _isbn(value: str) -> bool:
    compact = re.sub(r"[-\s]", "", value).upper()
    if len(compact) == 10:
        if not re.fullmatch(r"[0-9]{9}[0-9X]", compact):
            return False
        total = sum(
            (10 - index) * (10 if digit == "X" else int(digit))
            for index, digit in enumerate(compact)
        )
        return total % 11 == 0
    if len(compact) == 13 and compact.isdigit():
        total = sum(
            int(digit) * (1 if index % 2 == 0 else 3)
            for index, digit in enumerate(compact)
        )
        return total % 10 == 0
    return False


def _nested_custom(key: str, value: Any, path: str) -> dict:
    item = _expect_object(value, path)
    _reject_unknown(item, NESTED_CUSTOM_FIELDS[key], path)
    result: dict[str, Any] = {}
    for field_name, field_value in item.items():
        field_path = f"{path}.{field_name}"
        if field_name == "identifiers":
            result[field_name] = [
                _identifier_item(
                    entry, _json_path(field_path, index), IDENTIFIER_SCHEMES
                )
                for index, entry in enumerate(
                    _expect_list(field_value, field_path)
                )
            ]
        elif field_name == "url":
            url = _expect_string(field_value, field_path)
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ConfigError(f"{field_path}: expected an absolute http(s) URL")
            result[field_name] = url
        elif field_name in {"date_submitted", "date_defended"}:
            date = _expect_string(field_value, field_path)
            if not EDTF_RE.match(date):
                raise ConfigError(
                    f"{field_path}: expected an EDTF date or interval"
                )
            result[field_name] = date
        else:
            text = _expect_string(field_value, field_path)
            if field_name == "issn" and not _issn(text):
                raise ConfigError(f"{field_path}: invalid ISSN")
            if field_name == "isbn" and not _isbn(text):
                raise ConfigError(f"{field_path}: invalid ISBN")
            result[field_name] = text
    return result


def _normalise_custom(custom: Any, path: str) -> dict:
    data = _expect_object(custom, path)
    result: dict[str, Any] = {}
    for key, value in data.items():
        field_path = f"{path}.{key}"
        namespace = key.split(":", 1)[0] if ":" in key else ""
        if namespace not in CUSTOM_FIELD_NAMESPACES:
            raise ConfigError(
                f"{field_path}: unknown custom field namespace "
                f"'{namespace or key}'"
            )
        known = DOMAIN_CUSTOM_FIELDS.get(namespace)
        if known is not None and ":" in key:
            local = key.split(":", 1)[1]
            if local not in known:
                hint = ""
                matches = difflib.get_close_matches(local, sorted(known), n=3)
                if matches:
                    hint = f" (did you mean: {', '.join(matches)})"
                raise ConfigError(
                    f"{field_path}: unknown {namespace} custom field "
                    f"'{local}'{hint}"
                )
        if value is None:
            result[key] = None
        elif key == "code:codeRepository":
            url = _expect_string(value, field_path)
            if "<" in url or ">" in url:
                raise ConfigError(
                    f"{field_path}: remove the angle brackets around the URL"
                )
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ConfigError(
                    f"{field_path}: expected an absolute http(s) URL"
                )
            result[key] = url.rstrip("/")
        elif key == "code:programmingLanguage":
            result[key] = [
                _vocab_object(item, _json_path(field_path, index))
                for index, item in enumerate(_expect_list(value, field_path))
            ]
        elif key == "code:developmentStatus":
            result[key] = _vocab_object(value, field_path)
        elif key == "legacy:communities":
            result[key] = [
                _expect_string(item, _json_path(field_path, index))
                for index, item in enumerate(_expect_list(value, field_path))
            ]
        elif key == "legacy:subjects":
            items = []
            for index, entry in enumerate(_expect_list(value, field_path)):
                item_path = _json_path(field_path, index)
                item = _expect_object(entry, item_path)
                _reject_unknown(item, {"term", "identifier", "scheme"}, item_path)
                normalised = {"term": _expect_string(item.get("term"), f"{item_path}.term")}
                for field_name in ("identifier", "scheme"):
                    if field_name in item:
                        normalised[field_name] = _expect_string(
                            item[field_name], f"{item_path}.{field_name}"
                        )
                items.append(normalised)
            result[key] = items
        elif key in NESTED_CUSTOM_FIELDS:
            result[key] = _nested_custom(key, value, field_path)
        elif key == "obo:RO_0002453":
            relations = []
            for index, entry in enumerate(_expect_list(value, field_path)):
                item_path = _json_path(field_path, index)
                item = _expect_object(entry, item_path)
                _reject_unknown(item, {"subject", "object"}, item_path)
                relations.append(
                    {
                        "subject": _custom_list(
                            item.get("subject"), f"{item_path}.subject"
                        ),
                        "object": _custom_list(
                            item.get("object"), f"{item_path}.object"
                        ),
                    }
                )
            result[key] = relations
        elif namespace in DOMAIN_CUSTOM_NAMESPACES:
            if isinstance(value, list):
                result[key] = _custom_list(value, field_path)
            else:
                result[key] = [_custom_scalar(value, field_path)]
        else:
            raise ConfigError(f"{field_path}: unsupported custom field")
    return result


def _normalise_access(access: Any, path: str) -> dict:
    data = _expect_object(access, path)
    allowed = {"record", "files", "embargo"}
    unknown = set(data) - allowed
    if unknown:
        keys = ", ".join(sorted(unknown))
        raise ConfigError(f"{path}: unsupported key(s) {keys}")
    result: dict[str, Any] = {}
    for key in ("record", "files"):
        if key in data:
            value = _expect_string(data[key], f"{path}.{key}")
            if value not in {"public", "restricted"}:
                raise ConfigError(
                    f"{path}.{key}: expected 'public' or 'restricted'"
                )
            result[key] = value
    if "embargo" in data:
        embargo = _expect_object(data["embargo"], f"{path}.embargo")
        if not embargo.get("active"):
            result["embargo"] = {"active": False}
        else:
            reason = _expect_string(
                embargo.get("reason"), f"{path}.embargo.reason"
            )
            until = embargo.get("until")
            if until is not None:
                _expect_string(until, f"{path}.embargo.until")
            result["embargo"] = {
                "active": True,
                "reason": reason,
                **({"until": until} if until else {}),
            }
    return result


@dataclass
class Config:
    path: str
    repository: str | None = None
    record: str | None = None
    concept: str | None = None
    version: str | None = None
    metadata: dict = field(default_factory=dict)
    custom_fields: dict = field(default_factory=dict)
    access: dict | None = None
    files_enabled: bool | None = None


def _normalise_repository(value: Any, path: str) -> str:
    url = _expect_string(value, path).strip().rstrip("/")
    if url.endswith(".git"):
        url = url[: -len(".git")]
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError(f"{path}: expected an absolute http(s) URL")
    return url


def _normalise_identifier(value: Any, path: str) -> str:
    if isinstance(value, bool):
        raise ConfigError(f"{path}: expected a record identifier")
    if isinstance(value, int):
        return str(value)
    return _expect_string(value, path)


def load_config(path: str) -> Config:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(
            f"{path}: configuration file not found (set the 'config' input)"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read configuration: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{path}: invalid JSON at line {exc.lineno}, column {exc.colno}: "
            f"{exc.msg}"
        ) from exc
    _expect_object(raw, path)
    for key in raw:
        if key not in TOP_LEVEL_KEYS and key != "files":
            raise ConfigError(f"{path}.{key}: unsupported top-level key")

    config = Config(path=path)
    if "repository" in raw:
        config.repository = _normalise_repository(
            raw["repository"], f"{path}.repository"
        )
    if "record" in raw:
        config.record = _normalise_identifier(raw["record"], f"{path}.record")
    if "concept" in raw:
        config.concept = _normalise_identifier(raw["concept"], f"{path}.concept")
    if config.record and config.concept:
        raise ConfigError(
            f"{path}: declare either 'record' or 'concept', not both"
        )
    if "version" in raw:
        config.version = _expect_string(raw["version"], f"{path}.version")
    if "metadata" in raw:
        config.metadata = _normalise_metadata(raw["metadata"], f"{path}.metadata")
    if "custom_fields" in raw:
        config.custom_fields = _normalise_custom(
            raw["custom_fields"], f"{path}.custom_fields"
        )
    if "custom" in raw:
        legacy = _normalise_custom(raw["custom"], f"{path}.custom")
        for key, value in legacy.items():
            config.custom_fields.setdefault(key, value)
    if "access" in raw:
        config.access = _normalise_access(raw["access"], f"{path}.access")
    if "files" in raw:
        files = _expect_object(raw["files"], f"{path}.files")
        for key in files:
            if key != "enabled":
                raise ConfigError(f"{path}.files.{key}: unsupported key")
        if "enabled" in files:
            if not isinstance(files["enabled"], bool):
                raise ConfigError(f"{path}.files.enabled: expected a boolean")
            config.files_enabled = files["enabled"]
    return config


@dataclass
class Drift:
    section: str
    path: str
    current: Any
    desired: Any

    def key(self) -> str:
        return f"{self.section}.{self.path}"


def _matches(desired: Any, actual: Any) -> bool:
    if isinstance(desired, dict):
        if not isinstance(actual, dict):
            return False
        return all(key in actual and _matches(value, actual[key]) for key, value in desired.items())
    if isinstance(desired, list):
        if not isinstance(actual, list) or len(desired) != len(actual):
            return False
        return all(_matches(a, b) for a, b in zip(desired, actual))
    if desired is None:
        return actual is None
    if isinstance(desired, bool) or isinstance(actual, bool):
        return desired == actual
    return desired == actual


def _diff_section(section: str, current: dict, desired: dict) -> list[Drift]:
    drifts: list[Drift] = []
    for key, value in desired.items():
        actual = current.get(key)
        if not _matches(value, actual):
            drifts.append(Drift(section, key, actual, value))
    return drifts


def _communities_match(desired: list[str], actual: Any) -> bool:
    if not isinstance(actual, list):
        return False
    seen = {str(item) for item in actual}
    return all(str(item) in seen for item in desired)


def compute_drift(
    record_data: dict, config: Config
) -> list[Drift]:
    metadata = record_data.get("metadata") or {}
    custom = record_data.get("custom_fields") or {}
    drifts = _diff_section("metadata", metadata, config.metadata)
    for key, value in config.custom_fields.items():
        actual = custom.get(key)
        if key == "legacy:communities" and isinstance(value, list):
            if not _communities_match(value, actual):
                drifts.append(Drift("custom_fields", key, actual, value))
            continue
        if not _matches(value, actual):
            drifts.append(Drift("custom_fields", key, actual, value))
    return drifts


def _merge_section(current: dict | None, desired: dict) -> dict:
    merged = copy.deepcopy(current or {})
    for key, value in desired.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _sanitise_payload_metadata(metadata: dict) -> dict:
    """Remove server-derived fields that the draft PUT rejects.

    Zenodo returns richer objects than it accepts: affiliations come back with
    their identifiers and rights with icon/props, and a PUT echoing them fails
    with "Unknown field", "Duplicated affiliations", or "Only an existing id or
    free text ...". Strip those back to the writable form before sending.
    """
    cleaned = copy.deepcopy(metadata)
    for key in ("creators", "contributors"):
        for person in cleaned.get(key) or []:
            if not isinstance(person, dict):
                continue
            affiliations = person.get("affiliations")
            if not isinstance(affiliations, list):
                continue
            seen: set[str] = set()
            kept: list[dict] = []
            for affiliation in affiliations:
                if not isinstance(affiliation, dict):
                    continue
                if affiliation.get("id"):
                    item = {"id": affiliation["id"]}
                elif affiliation.get("name"):
                    item = {"name": affiliation["name"]}
                else:
                    continue
                marker = item.get("id") or item["name"]
                if marker in seen:
                    continue
                seen.add(marker)
                kept.append(item)
            if kept:
                person["affiliations"] = kept
            else:
                person.pop("affiliations", None)
    rights = cleaned.get("rights")
    if isinstance(rights, list):
        kept_rights = []
        for right in rights:
            if not isinstance(right, dict):
                continue
            if right.get("id"):
                kept_rights.append({"id": right["id"]})
                continue
            item = {
                field: right[field]
                for field in ("title", "description", "link")
                if field in right
            }
            if item:
                kept_rights.append(item)
        if kept_rights:
            cleaned["rights"] = kept_rights
        else:
            cleaned.pop("rights", None)
    return cleaned


@dataclass
class ZenodoRecord:
    data: dict
    reason: str

    @property
    def id(self) -> str:
        return str(self.data.get("id"))

    @property
    def concept_id(self) -> str | None:
        parent = self.data.get("parent") or {}
        return str(parent.get("id")) if parent.get("id") else None

    @property
    def version(self) -> str | None:
        return (self.data.get("metadata") or {}).get("version")

    @property
    def title(self) -> str | None:
        return (self.data.get("metadata") or {}).get("title")

    @property
    def url(self) -> str:
        return f"{self.client_base}/records/{self.id}"

    client_base: str = ""


class Resolver:
    def __init__(self, client: HttpClient):
        self.client = client

    def get(self, record_id: str, reason: str = "") -> ZenodoRecord:
        response = self.client.get(f"/api/records/{record_id}")
        data = response.json()
        if not isinstance(data, dict) or "id" not in data:
            raise ApiError(f"GET /api/records/{record_id} returned no record")
        return ZenodoRecord(data=data, reason=reason, client_base=self.client.base_url)

    def version_ids(self, record_id: str) -> list[dict]:
        hits: list[dict] = []
        page = 1
        while page <= 10:
            response = self.client.get(
                f"/api/records/{record_id}/versions",
                params={"size": 25, "page": page},
            )
            data = response.json() or {}
            batch = (data.get("hits") or {}).get("hits") or []
            hits.extend(batch)
            total = (data.get("hits") or {}).get("total")
            if not batch or not isinstance(total, int) or len(hits) >= total:
                break
            page += 1
        return hits

    def find_version(self, latest: ZenodoRecord, version: str) -> ZenodoRecord | None:
        matches: list[dict] = []
        if latest.version == version:
            matches.append(latest.data)
        for hit in self.version_ids(latest.id):
            if (hit.get("metadata") or {}).get("version") == version:
                matches.append(hit)
        unique = {str(hit.get("id")): hit for hit in matches}
        if len(unique) > 1:
            raise _duplicate_error(
                list(unique.values()),
                f"concept {latest.concept_id} version {version}",
            )
        if unique:
            identifier = next(iter(unique))
            if identifier == latest.id:
                return latest
            return self.get(
                identifier,
                f"version {version} of concept {latest.concept_id}",
            )
        return None

    def search(self, query: str, all_versions: bool) -> list[dict]:
        params: dict[str, Any] = {"q": query, "size": 25, "sort": "mostrecent"}
        if all_versions:
            params["allversions"] = "true"
        response = self.client.get("/api/records", params=params)
        data = response.json() or {}
        return (data.get("hits") or {}).get("hits") or []

    def by_version(self, repository: str, version: str) -> list[dict]:
        repo_clause = (
            f'custom_fields.code\\:codeRepository:"{repository}"'
        )
        related = f"{repository}/tree/{version}"
        related_clause = (
            'metadata.related_identifiers.identifier:'
            f'"{related}"'
        )
        query = (
            f"({repo_clause} OR {related_clause}) "
            f'AND metadata.version:"{version}"'
        )
        return self.search(query, all_versions=True)

    def by_repository(self, repository: str) -> list[dict]:
        repo_clause = (
            f'custom_fields.code\\:codeRepository:"{repository}"'
        )
        return self.search(repo_clause, all_versions=False)


@dataclass
class Resolution:
    record: ZenodoRecord | None
    latest: ZenodoRecord | None
    reason: str
    missing_version: str | None = None
    duplicates: list[dict] = field(default_factory=list)


def _duplicate_error(records: list[dict], label: str) -> DuplicateError:
    lines = []
    for record in records:
        identifier = record.get("id")
        version = (record.get("metadata") or {}).get("version") or "?"
        title = (record.get("metadata") or {}).get("title") or ""
        lines.append(
            f"  - {identifier} ({version}) https://zenodo.org/records/{identifier}"
            + (f" — {title}" if title else "")
        )
    error = DuplicateError(
        f"{len(records)} Zenodo records match {label}:\n" + "\n".join(lines)
        + "\nNever delete published records; contact Zenodo support to merge them."
    )
    error.records = records
    return error


def resolve_record(
    resolver: Resolver, config: Config, version: str | None
) -> Resolution:
    if config.record:
        record = resolver.get(config.record, f"explicit record {config.record}")
        if version and record.version and record.version != version:
            found = resolver.find_version(record, version)
            if found is None:
                raise ConfigError(
                    f"record {record.id} has version {record.version!r}, "
                    f"not {version!r}; point at the right record or use "
                    f"'concept'"
                )
            record = found
        _verify_anchor(record, config)
        return Resolution(record=record, latest=None, reason=record.reason)

    if config.concept:
        latest = resolver.get(config.concept, f"concept {config.concept}")
        _verify_anchor(latest, config)
        if version:
            found = resolver.find_version(latest, version)
            if found is not None:
                return Resolution(
                    record=found,
                    latest=None,
                    reason=f"version {version} of concept {config.concept}",
                )
            return Resolution(
                record=None,
                latest=latest,
                reason=f"concept {config.concept} has no version {version}",
                missing_version=version,
            )
        return Resolution(
            record=latest,
            latest=None,
            reason=f"latest version of concept {config.concept}",
        )

    if not config.repository:
        raise ConfigError(
            "cannot resolve a record: declare 'record', 'concept' or "
            "'repository' in the configuration"
        )

    if version:
        hits = resolver.by_version(config.repository, version)
        unique = {str(hit.get("id")): hit for hit in hits}
        if len(unique) > 1:
            raise _duplicate_error(
                list(unique.values()),
                f"repository {config.repository} version {version}",
            )
        if unique:
            identifier = next(iter(unique))
            record = resolver.get(
                identifier,
                f"repository {config.repository} version {version}",
            )
            _verify_anchor(record, config)
            return Resolution(record=record, latest=None, reason=record.reason)

    latest_hits = resolver.by_repository(config.repository)
    latest_unique = {str(hit.get("id")): hit for hit in latest_hits}
    if len(latest_unique) > 1:
        raise _duplicate_error(
            list(latest_unique.values()),
            f"repository {config.repository}",
        )
    if latest_unique:
        identifier = next(iter(latest_unique))
        latest = resolver.get(
            identifier, f"repository {config.repository}"
        )
        _verify_anchor(latest, config)
        if version and latest.version != version:
            return Resolution(
                record=None,
                latest=latest,
                reason=(
                    f"repository {config.repository} has no version {version}; "
                    f"latest is {latest.version or latest.id}"
                ),
                missing_version=version,
            )
        return Resolution(record=latest, latest=None, reason=latest.reason)

    return Resolution(
        record=None,
        latest=None,
        reason=f"no record found for repository {config.repository}",
        missing_version=version,
    )


def _verify_anchor(record: ZenodoRecord, config: Config) -> None:
    if not config.repository:
        return
    custom = record.data.get("custom_fields") or {}
    repository = custom.get("code:codeRepository")
    if isinstance(repository, str) and repository.rstrip("/") == config.repository:
        return
    related = (record.data.get("metadata") or {}).get("related_identifiers") or []
    needle = config.repository
    for item in related:
        identifier = item.get("identifier") if isinstance(item, dict) else None
        if isinstance(identifier, str) and identifier.startswith(needle):
            return
    raise ConfigError(
        f"record {record.id} is not linked to {config.repository} "
        f"(code:codeRepository={repository!r}); refusing to modify it"
    )


class Validator:
    def __init__(self, vocabularies: Vocabularies):
        self.vocabularies = vocabularies
        self.errors: list[str] = []

    def _check(self, kind: str, value: str, path: str) -> None:
        result = self.vocabularies.check(kind, value)
        if not result.supported:
            return
        if not result.exists:
            hint = f" (did you mean: {result.suggestion})" if result.suggestion else ""
            self.errors.append(f"{path}: unknown {kind} id {value!r}{hint}")

    def _check_metadata(self, metadata: dict) -> None:
        for key, value in metadata.items():
            if value is None:
                continue
            path = f"metadata.{key}"
            kind = VOCAB_ID_FIELDS.get(key)
            if kind:
                items = value if isinstance(value, list) else [value]
                for index, item in enumerate(items):
                    item_path = _json_path(path, index) if isinstance(value, list) else path
                    if not isinstance(item, dict):
                        continue
                    if item.get("id"):
                        self._check(kind, item["id"], f"{item_path}.id")
                    type_id = (item.get("type") or {}).get("id")
                    if type_id:
                        self._check(kind, type_id, f"{item_path}.type.id")
            if key in {"creators", "contributors"}:
                role_kind = "creator_role" if key == "creators" else "contributor_role"
                for index, item in enumerate(value):
                    role = item.get("role") or {}
                    if role.get("id"):
                        self._check(
                            role_kind, role["id"], f"{path}[{index}].role.id"
                        )
                    for position, affiliation in enumerate(
                        item.get("affiliations") or []
                    ):
                        affiliation_id = (affiliation or {}).get("id")
                        if affiliation_id:
                            self._check(
                                "affiliation",
                                affiliation_id,
                                f"{path}[{index}].affiliations[{position}].id",
                            )
            elif key == "related_identifiers":
                for index, item in enumerate(value):
                    relation = (item.get("relation_type") or {}).get("id")
                    if relation:
                        self._check(
                            "relation_type",
                            relation,
                            f"{path}[{index}].relation_type.id",
                        )
                    resource = (item.get("resource_type") or {}).get("id")
                    if resource:
                        self._check(
                            "resource_type",
                            resource,
                            f"{path}[{index}].resource_type.id",
                        )
            elif key == "funding":
                for index, item in enumerate(value):
                    funder = (item.get("funder") or {}).get("id")
                    if funder:
                        self._check(
                            "funder", funder, f"{path}[{index}].funder.id"
                        )
                    award = (item.get("award") or {}).get("id")
                    if award:
                        self._check(
                            "award", award, f"{path}[{index}].award.id"
                        )

    def _check_custom(self, custom: dict) -> None:
        for key, value in custom.items():
            if value is None:
                continue
            path = f"custom_fields.{key}"
            if key == "code:programmingLanguage":
                for index, item in enumerate(value):
                    self._check(
                        "code:programmingLanguage",
                        item["id"],
                        f"{path}[{index}].id",
                    )
            elif key == "code:developmentStatus":
                self._check(
                    "code:developmentStatus", value["id"], f"{path}.id"
                )
            elif key == "legacy:communities":
                for index, item in enumerate(value):
                    self._check("community", item, f"{path}[{index}]")

    def validate(self, config: Config) -> None:
        self._check_metadata(config.metadata)
        self._check_custom(config.custom_fields)
        if self.errors:
            detail = "\n".join(f"  - {error}" for error in self.errors)
            raise ConfigError(
                f"{config.path}: invalid declaration:\n{detail}"
            )


def _require_for_create(config: Config) -> None:
    missing = [key for key in REQUIRED_FOR_CREATE if not config.metadata.get(key)]
    if missing:
        names = ", ".join(f"metadata.{key}" for key in missing)
        raise ConfigError(
            f"{config.path}: creating a new deposit requires {names}"
        )


@dataclass
class SyncResult:
    status: str
    record: ZenodoRecord | None
    drift: list[Drift] = field(default_factory=list)
    message: str = ""
    duplicates: list[dict] = field(default_factory=list)
    revision_before: Any = None
    revision_after: Any = None


def _render_value(value: Any, limit: int = 160) -> str:
    if value is None:
        return "—"
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Engine:
    def __init__(
        self,
        client: HttpClient,
        config: Config,
        mode: str,
        dry_run: bool,
        version: str | None,
        publish_timeout: int = DEFAULT_PUBLISH_TIMEOUT,
    ):
        self.client = client
        self.config = config
        self.mode = mode
        self.dry_run = dry_run
        self.version = version
        self.publish_timeout = publish_timeout
        self.resolver = Resolver(client)
        self.vocabularies = Vocabularies(client)
        version = version or config.version
        self.version = version
        if version:
            config.metadata["version"] = version

    def run(self) -> SyncResult:
        Validator(self.vocabularies).validate(self.config)
        try:
            resolution = resolve_record(self.resolver, self.config, self.version)
        except DuplicateError as exc:
            return SyncResult(
                status="duplicates",
                record=None,
                message=str(exc),
                duplicates=getattr(exc, "records", []),
            )
        if resolution.record is not None:
            record = resolution.record
            drifts = compute_drift(record.data, self.config)
            if not drifts:
                return SyncResult(
                    status="in-sync",
                    record=record,
                    message=f"{resolution.reason}: already up to date",
                    revision_before=record.data.get("revision_id"),
                    revision_after=record.data.get("revision_id"),
                )
            if self.mode == "check" or self.dry_run:
                verb = "drifted" if self.mode == "check" else "would change"
                return SyncResult(
                    status="drift",
                    record=record,
                    drift=drifts,
                    message=(
                        f"{resolution.reason}: {len(drifts)} field(s) {verb}"
                    ),
                )
            return self._update(record, drifts, resolution.reason)

        if self.mode == "check" or self.dry_run:
            action = "missing" if resolution.latest is None else "new version needed"
            return SyncResult(
                status="missing",
                record=resolution.latest,
                message=f"{resolution.reason}: {action}",
                drift=[],
            )
        if resolution.latest is not None:
            return self._create_version(resolution.latest, resolution.reason)
        _require_for_create(self.config)
        return self._create_record(resolution.reason)

    def _payload_access(self, draft: dict | None = None) -> dict:
        source = draft or {}
        current = source.get("access") or {}
        access = {
            "record": current.get("record", "public"),
            "files": current.get("files", "public"),
        }
        embargo = current.get("embargo")
        if isinstance(embargo, dict) and embargo.get("active"):
            access["embargo"] = {
                "active": True,
                "reason": embargo.get("reason"),
            }
            if embargo.get("until"):
                access["embargo"]["until"] = embargo["until"]
        if self.config.access:
            for key in ("record", "files"):
                if key in self.config.access:
                    access[key] = self.config.access[key]
            if "embargo" in self.config.access:
                access["embargo"] = self.config.access["embargo"]
        return access

    def _payload_files(self, draft: dict | None = None) -> dict:
        source = (draft or {}).get("files") or {}
        enabled = self.config.files_enabled
        if enabled is None:
            enabled = bool(source.get("enabled", False))
        files: dict[str, Any] = {"enabled": enabled}
        for key in ("default_preview", "order"):
            if source.get(key):
                files[key] = source[key]
        return files

    def _merged(self, base: dict) -> tuple[dict, dict]:
        metadata = _merge_section(base.get("metadata"), self.config.metadata)
        custom = _merge_section(base.get("custom_fields"), self.config.custom_fields)
        return _sanitise_payload_metadata(metadata), custom

    def _ensure_draft(self, record_id: str) -> dict:
        try:
            return self.client.get(f"/api/records/{record_id}/draft", retries=0).json()
        except ApiError as exc:
            if exc.status != 404:
                raise
        try:
            return self.client.post(f"/api/records/{record_id}/draft").json()
        except ApiError as exc:
            if exc.status != 400:
                raise
            return self.client.get(f"/api/records/{record_id}/draft").json()

    def _update(
        self, record: ZenodoRecord, drifts: list[Drift], reason: str
    ) -> SyncResult:
        draft = self._ensure_draft(record.id)
        metadata, custom = self._merged(draft)
        body = {
            "access": self._payload_access(draft),
            "files": self._payload_files(draft),
            "metadata": metadata,
            "custom_fields": custom,
        }
        self.client.put(f"/api/records/{record.id}/draft", body)
        published = self._publish(record.id)
        residual = compute_drift(published, self.config)
        if residual:
            raise ApiError(
                f"record {record.id} still differs after sync: "
                + ", ".join(drift.key() for drift in residual)
            )
        return SyncResult(
            status="updated",
            record=ZenodoRecord(
                data=published,
                reason=reason,
                client_base=self.client.base_url,
            ),
            drift=drifts,
            message=f"{reason}: updated and published",
            revision_before=record.data.get("revision_id"),
            revision_after=published.get("revision_id"),
        )

    def _release_archive(self) -> tuple[str, str] | None:
        version = self.version or self.config.version
        if not self.config.repository or not version:
            return None
        parsed = urllib.parse.urlsplit(self.config.repository)
        if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
            return None
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2:
            return None
        owner, repo = parts[0], parts[1]
        key = f"{owner}/{repo}-{version}.zip"
        url = (
            f"https://github.com/{owner}/{repo}/archive/refs/tags/"
            f"{urllib.parse.quote(version, safe='')}.zip"
        )
        return key, url

    def _download_release_archive(self) -> tuple[str, bytes] | None:
        source = self._release_archive()
        if source is None:
            return None
        key, url = source
        handle, path = tempfile.mkstemp(suffix=".zip", prefix="zenodo-sync-")
        os.close(handle)
        try:
            try:
                self.client.download(url, path)
            except ApiError as exc:
                if exc.status == 404:
                    raise ApiError(
                        f"release archive not found at {url}; check that "
                        f"version {self.version or self.config.version!r} "
                        "matches a tag, or set 'files': {\"enabled\": false}"
                    ) from exc
                raise
            with open(path, "rb") as stream:
                return key, stream.read()
        finally:
            os.unlink(path)

    def _upload_release_archive(
        self, record_id: str, archive: tuple[str, bytes]
    ) -> str:
        key, data = archive
        quoted = urllib.parse.quote(key, safe="")
        self.client.post(f"/api/records/{record_id}/draft/files", [{"key": key}])
        self.client.put_bytes(
            f"/api/records/{record_id}/draft/files/{quoted}/content",
            data,
            "application/octet-stream",
        )
        self.client.post(
            f"/api/records/{record_id}/draft/files/{quoted}/commit"
        )
        return key

    def _discard_draft(self, record_id: str) -> None:
        try:
            self.client.delete(f"/api/records/{record_id}/draft")
        except ApiError as exc:
            if self.verbose:
                print(
                    f"::debug::could not discard draft {record_id}: {exc}",
                    file=sys.stderr,
                )

    def _discard_orphan_draft(self, error: ApiError) -> None:
        payload = error.payload if isinstance(error.payload, dict) else {}
        record_id = payload.get("id")
        if record_id and payload.get("is_draft"):
            self._discard_draft(str(record_id))

    def _create_record(self, reason: str) -> SyncResult:
        archive = (
            self._download_release_archive()
            if self.config.files_enabled is not False
            else None
        )
        body = {
            "access": self._payload_access(),
            "files": {
                "enabled": bool(self.config.files_enabled)
                or archive is not None
            },
            "metadata": copy.deepcopy(self.config.metadata),
            "custom_fields": {
                key: value
                for key, value in self.config.custom_fields.items()
                if value is not None
            },
        }
        try:
            response = self.client.post("/api/records", body)
        except ApiError as exc:
            self._discard_orphan_draft(exc)
            raise
        draft = response.json() or {}
        record_id = str(draft.get("id"))
        uploaded = None
        try:
            if archive is not None:
                uploaded = self._upload_release_archive(record_id, archive)
            published = self._publish(record_id)
        except ApiError:
            self._discard_draft(record_id)
            raise
        residual = compute_drift(published, self.config)
        if residual:
            raise ApiError(
                f"new record {record_id} still differs after sync: "
                + ", ".join(drift.key() for drift in residual)
            )
        return SyncResult(
            status="created",
            record=ZenodoRecord(
                data=published,
                reason=reason,
                client_base=self.client.base_url,
            ),
            message=(
                f"{reason}: created new deposit {record_id}"
                + (f" with {uploaded}" if uploaded else "")
            ),
            revision_after=published.get("revision_id"),
        )

    def _create_version(self, latest: ZenodoRecord, reason: str) -> SyncResult:
        archive = (
            self._download_release_archive()
            if self.config.files_enabled is not False
            else None
        )
        response = self.client.post(f"/api/records/{latest.id}/versions")
        draft = response.json() or {}
        new_id = str(draft.get("id"))
        current = self._ensure_draft(new_id)
        metadata, custom = self._merged(current)
        body = {
            "access": self._payload_access(current),
            "files": self._payload_files(current),
            "metadata": metadata,
            "custom_fields": custom,
        }
        self.client.put(f"/api/records/{new_id}/draft", body)
        uploaded = None
        if archive is not None:
            uploaded = self._upload_release_archive(new_id, archive)
        elif self.config.files_enabled is not False:
            files = latest.data.get("files") or {}
            if files.get("count") or files.get("entries"):
                self.client.post(
                    f"/api/records/{new_id}/draft/actions/files-import"
                )
        published = self._publish(new_id)
        residual = compute_drift(published, self.config)
        if residual:
            raise ApiError(
                f"new version {new_id} still differs after sync: "
                + ", ".join(drift.key() for drift in residual)
            )
        return SyncResult(
            status="created-version",
            record=ZenodoRecord(
                data=published,
                reason=reason,
                client_base=self.client.base_url,
            ),
            message=(
                f"{reason}: created version {new_id} from {latest.id}"
                + (f" with {uploaded}" if uploaded else "")
            ),
            revision_after=published.get("revision_id"),
        )

    def _publish(self, record_id: str) -> dict:
        self.client.post(f"/api/records/{record_id}/draft/actions/publish")
        deadline = time.monotonic() + self.publish_timeout
        while True:
            data = self.client.get(
                f"/api/records/{record_id}", retries=0
            ).json() or {}
            if data.get("is_published") and not data.get("is_draft"):
                return data
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PublishTimeout(
                    f"record {record_id} was not published within "
                    f"{self.publish_timeout}s; check "
                    f"{self.client.base_url}/records/{record_id}"
                )
            time.sleep(min(3.0, remaining))


def render_summary(result: SyncResult, mode: str, base_url: str) -> str:
    lines = [
        "### Zenodo schema sync",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Mode | `{mode}` |",
        f"| Status | `{result.status}` |",
    ]
    if result.record is not None:
        lines.append(f"| Record | [{result.record.id}]({base_url}/records/{result.record.id}) |")
        if result.record.version:
            lines.append(f"| Version | `{result.record.version}` |")
    if result.revision_before is not None or result.revision_after is not None:
        lines.append(
            f"| Revision | `{result.revision_before}` → `{result.revision_after}` |"
        )
    lines.append("")
    if result.message:
        lines.extend([result.message, ""])
    if result.drift:
        lines.extend(
            [
                "#### Drift",
                "",
                "| Field | Current | Declared |",
                "| --- | --- | --- |",
            ]
        )
        for drift in result.drift:
            lines.append(
                f"| `{drift.key()}` | `{_render_value(drift.current)}` | "
                f"`{_render_value(drift.desired)}` |"
            )
        lines.append("")
    if result.duplicates:
        lines.extend(
            [
                f"#### Duplicate deposits ({len(result.duplicates)})",
                "",
                "| Record | Version | URL |",
                "| --- | --- | --- |",
            ]
        )
        for record in result.duplicates:
            version = (record.get("metadata") or {}).get("version") or "?"
            lines.append(
                f"| `{record.get('id')}` | `{version}` | "
                f"{base_url}/records/{record.get('id')} |"
            )
        lines.append("")
        lines.append("Never delete published records; contact Zenodo support.")
        lines.append("")
    return "\n".join(lines)


def write_outputs(result: SyncResult) -> None:
    destination = os.environ.get("GITHUB_OUTPUT")
    if not destination:
        return
    owned = result.status in {
        "in-sync",
        "drift",
        "updated",
        "created",
        "created-version",
    }
    values = {
        "status": result.status,
        "record-id": result.record.id if owned and result.record else "",
        "record-url": result.record.url if owned and result.record else "",
    }
    try:
        with open(destination, "a", encoding="utf-8") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")
    except OSError as exc:
        print(f"warning: could not write outputs: {exc}", file=sys.stderr)


def write_summary(markdown: str, path: str | None = None) -> None:
    destination = path or os.environ.get("GITHUB_STEP_SUMMARY")
    if not destination:
        print(markdown)
        return
    try:
        with open(destination, "a", encoding="utf-8") as handle:
            handle.write(markdown + "\n")
    except OSError as exc:
        print(f"warning: could not write job summary: {exc}", file=sys.stderr)
        print(markdown)


def _token_from_environment() -> str | None:
    for name in ("ZENODO_TOKEN", "ZENODO_KEY"):
        value = os.environ.get(name)
        if value:
            return value.strip()
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zenodo_sync.py",
        description=(
            "Validate and apply current-schema metadata to a Zenodo record."
        ),
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--mode", choices=("check", "sync"), default="check")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--record", help="explicit Zenodo record id")
    target.add_argument("--concept", help="Zenodo concept (parent) id")
    parser.add_argument("--version", help="release version, e.g. v1.2.3")
    parser.add_argument("--repository", help="repository URL anchor")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--token", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--publish-timeout", type=int, default=DEFAULT_PUBLISH_TIMEOUT
    )
    parser.add_argument("--http-timeout", type=int, default=DEFAULT_HTTP_TIMEOUT)
    parser.add_argument("--summary-file", default=None)
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    token = (args.token or _token_from_environment() or "").strip() or None
    redactor = Redactor([token])
    config: Config | None = None
    result: SyncResult | None = None
    try:
        config = load_config(args.config)
        if args.repository:
            config.repository = _normalise_repository(
                args.repository, "--repository"
            )
        if args.record:
            config.record = _normalise_identifier(args.record, "--record")
            config.concept = None
        if args.concept:
            config.concept = _normalise_identifier(args.concept, "--concept")
            config.record = None
        version = args.version or config.version
        client = HttpClient(
            args.base_url,
            token=token,
            timeout=args.http_timeout,
            redactor=redactor,
            verbose=args.verbose,
        )
        engine = Engine(
            client=client,
            config=config,
            mode=args.mode,
            dry_run=args.dry_run,
            version=version,
            publish_timeout=args.publish_timeout,
        )
        result = engine.run()
        print(result.message)
        for drift in result.drift:
            print(
                f"drift {drift.key()}: current={_render_value(drift.current)} "
                f"declared={_render_value(drift.desired)}"
            )
        write_outputs(result)
        summary = render_summary(result, args.mode, args.base_url)
        write_summary(summary, args.summary_file)
        if result.status == "duplicates":
            return EXIT_DRIFT
        if args.mode == "check" and result.status in {"drift", "missing"}:
            return EXIT_DRIFT
        return EXIT_OK
    except DuplicateError as exc:
        print(f"::error::{redactor(str(exc))}", file=sys.stderr)
        result = SyncResult(
            status="duplicates",
            record=None,
            message=redactor(str(exc)),
            duplicates=getattr(exc, "records", []),
        )
        write_outputs(result)
        write_summary(render_summary(result, args.mode, args.base_url), args.summary_file)
        return exc.exit_code
    except SyncError as exc:
        message = redactor(str(exc))
        print(f"::error::{message}", file=sys.stderr)
        result = SyncResult(status="error", record=None, message=message)
        write_outputs(result)
        write_summary(render_summary(result, args.mode, args.base_url), args.summary_file)
        return exc.exit_code
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
