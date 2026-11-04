"""Offline tests for scripts/zenodo_sync.py.

No network access: every HTTP interaction goes through FakeClient, which also
asserts that check mode never writes.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import zenodo_sync as z  # noqa: E402

REPOSITORY = "https://github.com/example/project"


def make_record(
    record_id: str = "100",
    version: str = "v1.0.0",
    title: str = "Project",
    custom: dict | None = None,
    metadata: dict | None = None,
    published: bool = True,
) -> dict:
    base_metadata = {
        "title": title,
        "version": version,
        "publication_date": "2026-01-01",
        "resource_type": {"id": "software", "title": {"en": "Software"}},
        "creators": [
            {
                "person_or_org": {
                    "type": "personal",
                    "name": "Doe, Jane",
                    "given_name": "Jane",
                    "family_name": "Doe",
                }
            }
        ],
    }
    base_metadata.update(metadata or {})
    return {
        "id": record_id,
        "revision_id": 1,
        "is_published": published,
        "is_draft": not published,
        "parent": {"id": "10"},
        "versions": {"is_latest": True, "index": 1},
        "metadata": base_metadata,
        "custom_fields": custom
        if custom is not None
        else {"code:codeRepository": REPOSITORY, "legacy:communities": ["comm"]},
        "access": {
            "record": "public",
            "files": "public",
            "embargo": {"active": False},
        },
        "files": {"enabled": True, "count": 1, "entries": {}},
    }


class FakeClient(z.HttpClient):
    def __init__(self, records: dict[str, dict] | None = None):
        super().__init__("https://sandbox.example")
        self.records = {key: json.loads(json.dumps(value)) for key, value in (records or {}).items()}
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[tuple[str, str, object]] = []
        self.downloads: list[tuple[str, str]] = []
        self.uploads: list[tuple[str, str, bytes]] = []
        self.no_anonymous_writes = True

    def download(self, url, destination, headers=None, accept="application/octet-stream"):
        self.calls.append(("GET", url))
        self.downloads.append((url, destination))
        with open(destination, "wb") as handle:
            handle.write(b"PK\x05\x06" + b"\x00" * 18)

    def put_bytes(self, path, data, content_type):
        self.calls.append(("PUT", path))
        self.uploads.append((path, content_type, data))
        return z.Response(200, {}, b"{}", path)

    def get(self, path, accept=z.RDM_ACCEPT, retries=2, params=None):
        self.calls.append(("GET", path))
        if path.startswith("/api/vocabularies/") or path.startswith("/api/communities/"):
            return z.Response(200, {}, b"{}", path)
        if path == "/api/records" and params:
            return self._search(params)
        parts = [part for part in path.split("?")[0].split("/") if part]
        if len(parts) >= 3 and parts[:2] == ["api", "records"]:
            record_id = parts[2]
            if len(parts) == 3:
                record = self.records.get(record_id)
                if record is None:
                    raise z.ApiError("not found", status=404)
                if record.get("is_draft") and not record.get("is_published"):
                    raise z.ApiError("not found", status=404)
                return self._response(record)
            if len(parts) == 4 and parts[3] == "draft":
                draft = self.records.get(record_id)
                if draft is None or not draft.get("is_draft"):
                    raise z.ApiError("not found", status=404)
                return self._response(draft)
            if len(parts) == 4 and parts[3] == "versions":
                hits = [
                    value
                    for value in self.records.values()
                    if (value.get("parent") or {}).get("id")
                    == (self.records.get(record_id, {}).get("parent") or {}).get("id")
                ]
                return z.Response(
                    200,
                    {},
                    json.dumps({"hits": {"hits": hits, "total": len(hits)}}).encode(),
                    path,
                )
        raise z.ApiError(f"unexpected GET {path}", status=404)

    def post(self, path, body=None):
        self.calls.append(("POST", path))
        self.bodies.append(("POST", path, body))
        parts = [part for part in path.split("?")[0].split("/") if part]
        if path == "/api/records":
            record = make_record(
                record_id="200",
                version=(body or {}).get("metadata", {}).get("version", "v1.0.0"),
                custom=(body or {}).get("custom_fields"),
                metadata=(body or {}).get("metadata"),
                published=False,
            )
            self.records["200"] = record
            return self._response(record, status=201)
        if len(parts) == 4 and parts[3] == "draft":
            record = self.records[parts[2]]
            draft = json.loads(json.dumps(record))
            draft["is_draft"] = True
            draft["is_published"] = False
            draft["revision_id"] = record.get("revision_id", 1) + 1
            self.records[parts[2]] = draft
            return self._response(draft, status=201)
        if len(parts) == 6 and parts[3:5] == ["draft", "actions"] and parts[5] == "publish":
            record = self.records[parts[2]]
            record["is_draft"] = False
            record["is_published"] = True
            return self._response(record, status=202)
        if len(parts) == 4 and parts[3] == "versions":
            source = self.records[parts[2]]
            draft = make_record(
                record_id="300",
                version="v2.0.0",
                custom=source.get("custom_fields"),
                metadata=source.get("metadata"),
                published=False,
            )
            self.records["300"] = draft
            return self._response(draft, status=201)
        for suffix in ("/draft/actions/files-import",):
            if path.endswith(suffix):
                return z.Response(201, {}, b"{}", path)
        if len(parts) == 5 and parts[3] == "draft" and parts[4] == "files":
            record = self.records[parts[2]]
            entries = [{"key": item["key"], "status": "pending"} for item in body]
            return z.Response(
                201,
                {},
                json.dumps({"enabled": True, "entries": entries}).encode(),
                path,
            )
        if (
            len(parts) == 7
            and parts[3] == "draft"
            and parts[4] == "files"
            and parts[6] == "commit"
        ):
            return z.Response(
                200,
                {},
                json.dumps({"key": parts[5], "status": "completed"}).encode(),
                path,
            )
        raise z.ApiError(f"unexpected POST {path}", status=404)

    def put(self, path, body):
        self.calls.append(("PUT", path))
        self.bodies.append(("PUT", path, body))
        parts = [part for part in path.split("?")[0].split("/") if part]
        if len(parts) == 4 and parts[3] == "draft":
            record = self.records[parts[2]]
            record["metadata"] = body["metadata"]
            record["custom_fields"] = body["custom_fields"]
            record["access"] = body["access"]
            record["files"].update(body["files"])
            record["revision_id"] = record.get("revision_id", 1) + 1
            return self._response(record)
        raise z.ApiError(f"unexpected PUT {path}", status=404)

    def _search(self, params):
        query = params.get("q", "")
        hits = []
        for record in self.records.values():
            if not record.get("is_published"):
                continue
            metadata = record.get("metadata") or {}
            custom = record.get("custom_fields") or {}
            matched = False
            if REPOSITORY in query:
                if custom.get("code:codeRepository") == REPOSITORY:
                    matched = True
                for related in metadata.get("related_identifiers") or []:
                    if related.get("identifier", "").startswith(REPOSITORY):
                        matched = True
            if "metadata.version" in query:
                marker = query.split('metadata.version:"', 1)[1].split('"', 1)[0]
                matched = matched and metadata.get("version") == marker
            if matched:
                hits.append(record)
        if params.get("allversions") != "true":
            hits = [hit for hit in hits if (hit.get("versions") or {}).get("is_latest", True)]
        return z.Response(
            200,
            {},
            json.dumps({"hits": {"hits": hits, "total": len(hits)}}).encode(),
            "/api/records",
        )

    def _response(self, record: dict, status: int = 200) -> z.Response:
        return z.Response(status, {}, json.dumps(record).encode(), "/api/records")

    def delete(self, path):
        self.calls.append(("DELETE", path))
        parts = [part for part in path.split("?")[0].split("/") if part]
        if len(parts) == 4 and parts[:2] == ["api", "records"] and parts[3] == "draft":
            self.records.pop(parts[2], None)
            return z.Response(204, {}, b"", path)
        raise z.ApiError(f"unexpected DELETE {path}", status=404)

    def writes(self) -> list[tuple[str, str]]:
        return [call for call in self.calls if call[0] != "GET"]


def write_config(data: dict) -> str:
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(data, handle)
    handle.close()
    return handle.name


class ConfigTests(unittest.TestCase):
    def test_invalid_json_reports_path(self):
        path = write_config({})
        with open(path, "wb") as handle:
            handle.write(b"{")
        with self.assertRaises(z.ConfigError) as context:
            z.load_config(path)
        self.assertIn(path, str(context.exception))
        os.unlink(path)

    def test_unknown_key_reports_path(self):
        path = write_config({"record": "1", "metadata": {"nope": 1}})
        with self.assertRaises(z.ConfigError) as context:
            z.load_config(path)
        self.assertIn("metadata.nope", str(context.exception))
        os.unlink(path)

    def test_angle_bracket_url_rejected(self):
        path = write_config(
            {"record": "1", "custom_fields": {"code:codeRepository": "<https://x/y>"}}
        )
        with self.assertRaises(z.ConfigError):
            z.load_config(path)
        os.unlink(path)

    def test_string_vocab_values_become_ids(self):
        path = write_config(
            {
                "record": "1",
                "metadata": {"resource_type": "software"},
                "custom_fields": {"code:developmentStatus": "active"},
            }
        )
        config = z.load_config(path)
        self.assertEqual(config.metadata["resource_type"], {"id": "software"})
        self.assertEqual(config.custom_fields["code:developmentStatus"], {"id": "active"})
        os.unlink(path)

    def test_additional_title_lang_becomes_id(self):
        path = write_config(
            {
                "record": "1",
                "metadata": {
                    "additional_titles": [
                        {"title": "Sub", "type": "subtitle", "lang": "eng"}
                    ]
                },
            }
        )
        config = z.load_config(path)
        item = config.metadata["additional_titles"][0]
        self.assertEqual(item["type"], {"id": "subtitle"})
        self.assertEqual(item["lang"], {"id": "eng"})
        os.unlink(path)

    def test_locations_and_references_pass_through(self):
        locations = {"features": [{"place": "Cologne"}]}
        references = [{"reference": "Ref", "identifier": "10.1/x", "scheme": "doi"}]
        path = write_config(
            {
                "record": "1",
                "metadata": {"locations": locations, "references": references},
            }
        )
        config = z.load_config(path)
        self.assertEqual(config.metadata["locations"], locations)
        self.assertEqual(config.metadata["references"], references)
        os.unlink(path)

    def test_record_and_concept_conflict(self):
        path = write_config({"record": "1", "concept": "2"})
        with self.assertRaises(z.ConfigError):
            z.load_config(path)
        os.unlink(path)

    def test_repository_url_normalised(self):
        path = write_config({"repository": REPOSITORY + ".git/"})
        config = z.load_config(path)
        self.assertEqual(config.repository, REPOSITORY)
        os.unlink(path)


class NormalisationTests(unittest.TestCase):
    def load(self, payload: dict) -> z.Config:
        path = write_config({"record": "1", **payload})
        try:
            return z.load_config(path)
        finally:
            os.unlink(path)

    def test_person_name_becomes_family_given(self):
        config = self.load(
            {
                "metadata": {
                    "creators": [
                        {"person_or_org": {"type": "personal", "name": "Doe, Jane"}}
                    ]
                }
            }
        )
        person = config.metadata["creators"][0]["person_or_org"]
        self.assertEqual(
            person,
            {
                "type": "personal",
                "name": "Doe, Jane",
                "given_name": "Jane",
                "family_name": "Doe",
            },
        )

    def test_personal_needs_family_name(self):
        path = write_config(
            {
                "record": "1",
                "metadata": {
                    "creators": [{"person_or_org": {"type": "personal", "name": "Jane"}}]
                },
            }
        )
        with self.assertRaises(z.ConfigError) as context:
            z.load_config(path)
        self.assertIn("family_name", str(context.exception))
        os.unlink(path)

    def test_person_type_is_required(self):
        path = write_config(
            {
                "record": "1",
                "metadata": {"creators": [{"person_or_org": {"name": "Doe, Jane"}}]},
            }
        )
        with self.assertRaises(z.ConfigError):
            z.load_config(path)
        os.unlink(path)

    def test_organizational_person_derives_name(self):
        config = self.load(
            {
                "metadata": {
                    "creators": [
                        {
                            "person_or_org": {
                                "type": "organizational",
                                "given_name": "CERN",
                                "family_name": "Lab",
                            }
                        }
                    ]
                }
            }
        )
        person = config.metadata["creators"][0]["person_or_org"]
        self.assertEqual(person["name"], "CERN Lab")
        self.assertNotIn("given_name", person)
        self.assertNotIn("family_name", person)

    def test_contributor_role_required(self):
        path = write_config(
            {
                "record": "1",
                "metadata": {
                    "contributors": [
                        {"person_or_org": {"type": "personal", "family_name": "Doe"}}
                    ]
                },
            }
        )
        with self.assertRaises(z.ConfigError) as context:
            z.load_config(path)
        self.assertIn("role", str(context.exception))
        os.unlink(path)

    def test_affiliations_accept_strings_and_objects(self):
        config = self.load(
            {
                "metadata": {
                    "creators": [
                        {
                            "person_or_org": {
                                "type": "personal",
                                "family_name": "Doe",
                            },
                            "affiliations": ["CERN", {"id": "05a28rw58"}, {"name": "ETH"}],
                        }
                    ]
                }
            }
        )
        self.assertEqual(
            config.metadata["creators"][0]["affiliations"],
            [{"name": "CERN"}, {"id": "05a28rw58"}, {"name": "ETH"}],
        )

    def test_affiliation_url_rejected(self):
        with self.assertRaises(z.ConfigError) as context:
            self.load(
                {
                    "metadata": {
                        "creators": [
                            {
                                "person_or_org": {"type": "personal", "family_name": "Doe"},
                                "affiliations": [{"id": "https://ror.org/01"}],
                            }
                        ]
                    }
                }
            )
        self.assertIn("use the Zenodo affiliation id, not a URL", str(context.exception))

    def test_duplicate_affiliations_rejected(self):
        path = write_config(
            {
                "record": "1",
                "metadata": {
                    "creators": [
                        {
                            "person_or_org": {"type": "personal", "family_name": "Doe"},
                            "affiliations": ["CERN", {"name": "CERN"}],
                        }
                    ]
                },
            }
        )
        with self.assertRaises(z.ConfigError) as context:
            z.load_config(path)
        self.assertIn("duplicate", str(context.exception))
        os.unlink(path)

    def test_rights_id_and_free_text(self):
        config = self.load(
            {
                "metadata": {
                    "rights": [
                        "cc-by-4.0",
                        {
                            "title": "Custom licence",
                            "description": {"en": "Short text"},
                            "link": "https://example.org/licence",
                        },
                    ]
                }
            }
        )
        rights = config.metadata["rights"]
        self.assertEqual(rights[0], {"id": "cc-by-4.0"})
        self.assertEqual(rights[1]["title"], {"en": "Custom licence"})

    def test_rights_id_with_title_rejected(self):
        path = write_config(
            {
                "record": "1",
                "metadata": {"rights": [{"id": "cc-by-4.0", "title": "Both"}]},
            }
        )
        with self.assertRaises(z.ConfigError):
            z.load_config(path)
        os.unlink(path)

    def test_funding_funder_and_award(self):
        config = self.load(
            {
                "metadata": {
                    "funding": [
                        {
                            "funder": "04jsz6e67",
                            "award": {
                                "number": "101057090",
                                "title": {"en": "A project"},
                                "identifiers": [
                                    {"identifier": "10.3030/101057090", "scheme": "doi"}
                                ],
                            },
                        },
                        {"funder": {"name": "Private funder"}},
                    ]
                }
            }
        )
        funding = config.metadata["funding"]
        self.assertEqual(funding[0]["funder"], {"id": "04jsz6e67"})
        self.assertEqual(funding[0]["award"]["number"], "101057090")
        self.assertEqual(funding[1]["funder"], {"name": "Private funder"})

    def test_funder_url_rejected(self):
        with self.assertRaises(z.ConfigError) as context:
            self.load(
                {
                    "metadata": {
                        "funding": [
                            {"funder": "https://doi.org/10.13039/501100000780"}
                        ]
                    }
                }
            )
        self.assertIn("use the Zenodo funder id, not a URL", str(context.exception))

    def test_award_acronym_and_program_rejected(self):
        with self.assertRaises(z.ConfigError) as context:
            self.load(
                {
                    "metadata": {
                        "funding": [
                            {
                                "funder": "04jsz6e67",
                                "award": {"number": "1", "acronym": "X"},
                            }
                        ]
                    }
                }
            )
        self.assertIn("unsupported key(s) acronym", str(context.exception))

    def test_reference_string_and_identifier(self):
        config = self.load(
            {
                "metadata": {
                    "references": [
                        "See also the project website",
                        {"reference": "A paper", "identifier": "10.1/x"},
                    ]
                }
            }
        )
        self.assertEqual(
            config.metadata["references"],
            [
                {"reference": "See also the project website"},
                {"reference": "A paper", "identifier": "10.1/x"},
            ],
        )

    def test_identifier_scheme_allowlist(self):
        path = write_config(
            {
                "record": "1",
                "metadata": {"identifiers": [{"identifier": "x", "scheme": "nope"}]},
            }
        )
        with self.assertRaises(z.ConfigError) as context:
            z.load_config(path)
        self.assertIn("scheme", str(context.exception))
        os.unlink(path)

    def test_edtf_date_and_interval(self):
        config = self.load(
            {
                "metadata": {
                    "dates": [
                        {"date": "2024-01-01", "type": "created"},
                        {"date": "2020/2021", "type": "valid"},
                    ]
                }
            }
        )
        self.assertEqual(config.metadata["dates"][1]["date"], "2020/2021")

    def test_invalid_date_rejected(self):
        path = write_config(
            {
                "record": "1",
                "metadata": {"dates": [{"date": "notadate", "type": "created"}]},
            }
        )
        with self.assertRaises(z.ConfigError):
            z.load_config(path)
        os.unlink(path)

    def test_locations_geometry(self):
        config = self.load(
            {
                "metadata": {
                    "locations": {
                        "features": [
                            {
                                "geometry": {"type": "Point", "coordinates": [6.9, 50.9]},
                                "place": "Cologne",
                                "identifiers": [
                                    {"identifier": "2886242", "scheme": "geonames"}
                                ],
                            }
                        ]
                    }
                }
            }
        )
        feature = config.metadata["locations"]["features"][0]
        self.assertEqual(feature["geometry"]["coordinates"], [6.9, 50.9])

    def test_multipolygon_rejected(self):
        path = write_config(
            {
                "record": "1",
                "metadata": {
                    "locations": {
                        "features": [
                            {"geometry": {"type": "MultiPolygon", "coordinates": []}}
                        ]
                    }
                },
            }
        )
        with self.assertRaises(z.ConfigError):
            z.load_config(path)
        os.unlink(path)

    def test_multipoint_rejected(self):
        path = write_config(
            {
                "record": "1",
                "metadata": {
                    "locations": {
                        "features": [
                            {
                                "geometry": {
                                    "type": "MultiPoint",
                                    "coordinates": [[6.9, 50.9], [7.0, 51.0]],
                                }
                            }
                        ]
                    }
                },
            }
        )
        with self.assertRaises(z.ConfigError) as context:
            z.load_config(path)
        self.assertIn("MultiPoint", str(context.exception))
        self.assertIn("broken record page", str(context.exception))
        os.unlink(path)

    def test_empty_feature_rejected(self):
        path = write_config(
            {"record": "1", "metadata": {"locations": {"features": [{}]}}}
        )
        with self.assertRaises(z.ConfigError):
            z.load_config(path)
        os.unlink(path)

    def test_version_length_limit(self):
        path = write_config({"record": "1", "metadata": {"version": "v" * 192}})
        with self.assertRaises(z.ConfigError):
            z.load_config(path)
        os.unlink(path)

    def test_nested_custom_fields(self):
        config = self.load(
            {
                "custom_fields": {
                    "journal:journal": {
                        "title": "Journal",
                        "volume": "1",
                        "issn": "2077-9550",
                    },
                    "meeting:meeting": {
                        "title": "Conference",
                        "url": "https://example.org/conf",
                        "identifiers": [{"identifier": "10.1/x", "scheme": "doi"}],
                    },
                    "imprint:imprint": {"title": "Book", "isbn": "978-3-16-148410-0"},
                    "thesis:thesis": {
                        "university": "TU Wien",
                        "date_submitted": "2024-02-01",
                    },
                }
            }
        )
        custom = config.custom_fields
        self.assertEqual(custom["journal:journal"]["issn"], "2077-9550")
        self.assertEqual(custom["meeting:meeting"]["url"], "https://example.org/conf")
        self.assertEqual(custom["imprint:imprint"]["isbn"], "978-3-16-148410-0")
        self.assertEqual(custom["thesis:thesis"]["date_submitted"], "2024-02-01")

    def test_nested_custom_field_unknown_key_rejected(self):
        path = write_config(
            {
                "record": "1",
                "custom_fields": {"journal:journal": {"title": "J", "nope": "x"}},
            }
        )
        with self.assertRaises(z.ConfigError):
            z.load_config(path)
        os.unlink(path)

    def test_invalid_issn_rejected(self):
        path = write_config(
            {
                "record": "1",
                "custom_fields": {"journal:journal": {"issn": "1234-5678"}},
            }
        )
        with self.assertRaises(z.ConfigError):
            z.load_config(path)
        os.unlink(path)

    def test_domain_custom_fields_become_lists(self):
        config = self.load(
            {
                "custom_fields": {
                    "dwc:kingdom": "Animalia",
                    "dwc:decimalLatitude": 50.9,
                    "dc:creator": ["Doe, Jane"],
                }
            }
        )
        self.assertEqual(config.custom_fields["dwc:kingdom"], ["Animalia"])
        self.assertEqual(config.custom_fields["dwc:decimalLatitude"], [50.9])
        self.assertEqual(config.custom_fields["dc:creator"], ["Doe, Jane"])

    def test_unknown_domain_custom_field_rejected(self):
        with self.assertRaises(z.ConfigError) as context:
            self.load({"custom_fields": {"gbif:DNAderivedData": "genomic DNA"}})
        message = str(context.exception)
        self.assertIn("unknown gbif custom field 'DNAderivedData'", message)

    def test_obo_relations(self):
        config = self.load(
            {
                "custom_fields": {
                    "obo:RO_0002453": [
                        {"subject": ["taxon A"], "object": ["habitat B"]}
                    ]
                }
            }
        )
        self.assertEqual(
            config.custom_fields["obo:RO_0002453"],
            [{"subject": ["taxon A"], "object": ["habitat B"]}],
        )

    def test_legacy_subjects(self):
        config = self.load(
            {
                "custom_fields": {
                    "legacy:subjects": [
                        {"term": "Physics", "identifier": "Q1", "scheme": "wikidata"}
                    ]
                }
            }
        )
        self.assertEqual(
            config.custom_fields["legacy:subjects"][0]["term"], "Physics"
        )


class ResponseErrorTests(unittest.TestCase):
    def client(self) -> z.HttpClient:
        return z.HttpClient("https://example.org")

    def response(self, payload: dict, status: int = 200) -> z.Response:
        return z.Response(status, {}, json.dumps(payload).encode(), "/api/records")

    def test_2xx_errors_raise(self):
        client = self.client()
        response = self.response(
            {
                "errors": [
                    {
                        "field": "metadata.title",
                        "messages": ["Too short."],
                    }
                ]
            }
        )
        with self.assertRaises(z.ApiError) as context:
            client.check_response_errors("POST", "/api/records", response)
        self.assertIn("metadata.title", str(context.exception))
        self.assertIn("Too short.", str(context.exception))

    def test_2xx_ignores_files_enabled(self):
        client = self.client()
        response = self.response(
            {
                "errors": [
                    {
                        "field": "files.enabled",
                        "messages": [
                            "You don't have permissions to manage files options."
                        ],
                    }
                ]
            }
        )
        client.check_response_errors("PUT", "/api/records/1/draft", response)

    def test_2xx_without_errors_passes(self):
        client = self.client()
        client.check_response_errors("POST", "/api/records", self.response({"id": "1"}))

    def test_non_2xx_ignored_by_check(self):
        client = self.client()
        response = self.response({"errors": [{"field": "x", "messages": ["y"]}]}, 400)
        client.check_response_errors("POST", "/api/records", response)


class DriftTests(unittest.TestCase):
    def config(self, **kwargs) -> z.Config:
        return z.Config(path="test.json", **kwargs)

    def test_no_drift_when_declared_is_subset(self):
        record = make_record()
        config = self.config(
            metadata={"title": "Project", "resource_type": {"id": "software"}},
            custom_fields={
                "code:codeRepository": REPOSITORY,
                "legacy:communities": ["comm"],
            },
        )
        self.assertEqual(z.compute_drift(record, config), [])

    def test_title_drift(self):
        record = make_record()
        config = self.config(metadata={"title": "Other"})
        drift = z.compute_drift(record, config)
        self.assertEqual([item.key() for item in drift], ["metadata.title"])

    def test_undeclared_fields_ignored(self):
        record = make_record()
        config = self.config(custom_fields={"code:codeRepository": REPOSITORY})
        self.assertEqual(z.compute_drift(record, config), [])

    def test_communities_superset_matches(self):
        record = make_record(
            custom={"code:codeRepository": REPOSITORY, "legacy:communities": ["a", "b"]}
        )
        config = self.config(custom_fields={"legacy:communities": ["b"]})
        self.assertEqual(z.compute_drift(record, config), [])

    def test_list_order_matters(self):
        record = make_record(
            custom={"code:programmingLanguage": [{"id": "python"}, {"id": "shell"}]}
        )
        config = self.config(
            custom_fields={"code:programmingLanguage": [{"id": "shell"}, {"id": "python"}]}
        )
        self.assertEqual(len(z.compute_drift(record, config)), 1)


class PayloadSanitiseTests(unittest.TestCase):
    def test_affiliations_keep_id_or_name_only(self):
        metadata = {
            "creators": [
                {
                    "person_or_org": {"type": "personal", "name": "Doe, Jane"},
                    "affiliations": [
                        {"name": "CERN"},
                        {
                            "id": "05a28rw58",
                            "name": "ETH Zurich",
                            "identifiers": [
                                {"identifier": "05a28rw58", "scheme": "ror"}
                            ],
                        },
                        {"name": "CERN"},
                    ],
                }
            ]
        }
        cleaned = z._sanitise_payload_metadata(metadata)
        affiliations = cleaned["creators"][0]["affiliations"]
        self.assertEqual(
            affiliations, [{"name": "CERN"}, {"id": "05a28rw58"}]
        )

    def test_rights_keep_id_or_free_text_only(self):
        metadata = {
            "rights": [
                {
                    "id": "cc-by-4.0",
                    "title": {"en": "Creative Commons"},
                    "icon": "cc-by-icon",
                    "props": {"url": "https://example.org"},
                },
                {
                    "title": {"en": "Custom licence"},
                    "description": {"en": "Free text"},
                    "link": "https://example.org/licence",
                },
            ]
        }
        cleaned = z._sanitise_payload_metadata(metadata)
        self.assertEqual(
            cleaned["rights"],
            [
                {"id": "cc-by-4.0"},
                {
                    "title": {"en": "Custom licence"},
                    "description": {"en": "Free text"},
                    "link": "https://example.org/licence",
                },
            ],
        )

    def test_does_not_mutate_input(self):
        metadata = {
            "creators": [
                {
                    "person_or_org": {"type": "personal", "name": "Doe, Jane"},
                    "affiliations": [{"id": "1", "name": "Example"}],
                }
            ]
        }
        z._sanitise_payload_metadata(metadata)
        self.assertEqual(
            metadata["creators"][0]["affiliations"],
            [{"id": "1", "name": "Example"}],
        )


class EngineTests(unittest.TestCase):
    def engine(self, client, config, mode="check", dry_run=False, version=None):
        return z.Engine(client, config, mode, dry_run, version)

    def test_check_reports_drift_without_writes(self):
        record = make_record()
        client = FakeClient({"100": record})
        config = z.Config(path="test.json", record="100", metadata={"title": "Other"})
        result = self.engine(client, config).run()
        self.assertEqual(result.status, "drift")
        self.assertEqual(client.writes(), [])

    def test_check_in_sync_exits_zero(self):
        record = make_record()
        client = FakeClient({"100": record})
        config = z.Config(path="test.json", record="100", metadata={"title": "Project"})
        result = self.engine(client, config).run()
        self.assertEqual(result.status, "in-sync")
        self.assertEqual(result.revision_before, result.revision_after)
        self.assertEqual(client.writes(), [])

    def test_sync_merges_undeclared_fields_and_publishes(self):
        record = make_record(
            metadata={"publisher": "Keep Me"},
            custom={"code:codeRepository": REPOSITORY, "legacy:communities": ["a"]},
        )
        client = FakeClient({"100": record})
        config = z.Config(
            path="test.json",
            record="100",
            metadata={"title": "Project"},
            custom_fields={"legacy:communities": ["a", "b"]},
        )
        result = self.engine(client, config, mode="sync").run()
        self.assertEqual(result.status, "updated")
        published = client.records["100"]
        self.assertEqual(published["metadata"]["publisher"], "Keep Me")
        self.assertEqual(published["custom_fields"]["legacy:communities"], ["a", "b"])
        self.assertTrue(published["is_published"])
        self.assertNotEqual(result.revision_before, result.revision_after)

    def test_sync_sanitises_draft_derived_fields(self):
        record = make_record(
            metadata={
                "publisher": "Keep Me",
                "creators": [
                    {
                        "person_or_org": {"type": "personal", "name": "Doe, Jane"},
                        "affiliations": [
                            {"name": "CERN"},
                            {"id": "1", "name": "Example", "identifiers": []},
                        ],
                    }
                ],
                "rights": [
                    {
                        "id": "cc-by-4.0",
                        "title": {"en": "Creative Commons"},
                        "icon": "cc-by-icon",
                    }
                ],
            }
        )
        client = FakeClient({"100": record})
        config = z.Config(
            path="test.json", record="100", metadata={"title": "Other"}
        )
        result = self.engine(client, config, mode="sync").run()
        self.assertEqual(result.status, "updated")
        sent = [body for method, path, body in client.bodies if method == "PUT"][0]
        self.assertEqual(
            sent["metadata"]["creators"][0]["affiliations"],
            [{"name": "CERN"}, {"id": "1"}],
        )
        self.assertEqual(sent["metadata"]["rights"], [{"id": "cc-by-4.0"}])
        self.assertEqual(sent["metadata"]["publisher"], "Keep Me")

    def test_second_sync_is_noop(self):
        record = make_record()
        client = FakeClient({"100": record})
        config = z.Config(path="test.json", record="100", metadata={"title": "Project"})
        self.engine(client, config, mode="sync").run()
        writes_after_first = client.writes()
        result = self.engine(client, config, mode="sync").run()
        self.assertEqual(result.status, "in-sync")
        self.assertEqual(client.writes(), writes_after_first)

    def test_sync_creates_missing_deposit_and_polls(self):
        client = FakeClient({})
        config = z.Config(
            path="test.json",
            repository=REPOSITORY,
            metadata={
                "title": "Project",
                "creators": z.load_config(
                    write_config(
                        {
                            "record": "x",
                            "metadata": {
                                "title": "Project",
                                "creators": [
                                    {
                                        "person_or_org": {
                                            "type": "personal",
                                            "family_name": "Doe",
                                            "given_name": "Jane",
                                        }
                                    }
                                ],
                            },
                        }
                    )
                ).metadata["creators"],
                "resource_type": {"id": "software"},
                "publication_date": "2026-01-01",
                "publisher": "Zenodo",
            },
            custom_fields={"code:codeRepository": REPOSITORY},
        )
        result = self.engine(client, config, mode="sync", version="v1.0.0").run()
        self.assertEqual(result.status, "created")
        self.assertTrue(client.records["200"]["is_published"])
        self.assertEqual(
            client.records["200"]["metadata"]["version"], "v1.0.0"
        )

    def test_missing_version_creates_new_version_from_latest(self):
        latest = make_record(version="v1.0.0")
        client = FakeClient({"100": latest})
        config = z.Config(
            path="test.json",
            repository=REPOSITORY,
            metadata={"title": "Project"},
            custom_fields={"code:codeRepository": REPOSITORY},
        )
        result = self.engine(client, config, mode="sync", version="v2.0.0").run()
        self.assertEqual(result.status, "created-version")
        self.assertTrue(any(call[1].endswith("/versions") for call in client.calls))
        self.assertTrue(client.records["300"]["is_published"])

    def test_missing_version_uploads_release_archive(self):
        latest = make_record(version="v1.0.0")
        client = FakeClient({"100": latest})
        config = z.Config(
            path="test.json",
            repository=REPOSITORY,
            metadata={"title": "Project"},
        )
        result = self.engine(client, config, mode="sync", version="v2.0.0").run()
        self.assertEqual(result.status, "created-version")
        self.assertEqual(
            [url for url, _ in client.downloads],
            [
                "https://github.com/example/project/archive/refs/tags/"
                "v2.0.0.zip"
            ],
        )
        keys = [
            body[0]["key"]
            for method, path, body in client.bodies
            if method == "POST" and path.endswith("/draft/files")
        ]
        self.assertEqual(keys, ["example/project-v2.0.0.zip"])
        self.assertEqual(
            [path for path, _, _ in client.uploads],
            [
                "/api/records/300/draft/files/"
                "example%2Fproject-v2.0.0.zip/content"
            ],
        )
        self.assertTrue(
            any(
                path.endswith(
                    "/draft/files/example%2Fproject-v2.0.0.zip/commit"
                )
                for _, path in client.calls
            )
        )
        self.assertFalse(
            any(
                path.endswith("/draft/actions/files-import")
                for _, path in client.calls
            )
        )
        self.assertIn("example/project-v2.0.0.zip", result.message)

    def test_new_deposit_uploads_release_archive(self):
        client = FakeClient({})
        config = z.Config(
            path="test.json",
            repository=REPOSITORY,
            metadata={
                "title": "Project",
                "creators": [
                    {
                        "person_or_org": {
                            "type": "personal",
                            "family_name": "Doe",
                            "given_name": "Jane",
                        }
                    }
                ],
                "resource_type": {"id": "software"},
                "publication_date": "2026-01-01",
                "publisher": "Zenodo",
            },
        )
        result = self.engine(client, config, mode="sync", version="v1.0.0").run()
        self.assertEqual(result.status, "created")
        self.assertEqual(len(client.uploads), 1)
        self.assertEqual(
            client.downloads[0][0],
            "https://github.com/example/project/archive/refs/tags/"
            "v1.0.0.zip",
        )
        self.assertIn("example/project-v1.0.0.zip", result.message)

    def test_files_disabled_skips_upload_and_import(self):
        latest = make_record(version="v1.0.0")
        client = FakeClient({"100": latest})
        config = z.Config(
            path="test.json",
            repository=REPOSITORY,
            metadata={"title": "Project"},
            files_enabled=False,
        )
        self.engine(client, config, mode="sync", version="v2.0.0").run()
        self.assertEqual(client.downloads, [])
        self.assertFalse(
            any(
                "files-import" in path or path.endswith("/draft/files")
                for _, path in client.calls
            )
        )

    def test_missing_archive_tag_fails_before_publish(self):
        client = FakeClient({})
        config = z.Config(
            path="test.json",
            repository=REPOSITORY,
            metadata={
                "title": "Project",
                "creators": [
                    {
                        "person_or_org": {
                            "type": "personal",
                            "family_name": "Doe",
                            "given_name": "Jane",
                        }
                    }
                ],
                "resource_type": {"id": "software"},
                "publication_date": "2026-01-01",
                "publisher": "Zenodo",
            },
        )

        def missing(url, destination, headers=None, accept="application/octet-stream"):
            raise z.ApiError("not found", status=404)

        client.download = missing
        with self.assertRaises(z.ApiError) as context:
            self.engine(client, config, mode="sync", version="v9.9.9").run()
        self.assertIn("release archive not found", str(context.exception))
        self.assertEqual(client.writes(), [])

    def test_duplicates_reported_without_writes(self):
        first = make_record(record_id="100")
        second = make_record(record_id="101")
        client = FakeClient({"100": first, "101": second})
        config = z.Config(
            path="test.json",
            repository=REPOSITORY,
            metadata={"title": "Project"},
        )
        result = self.engine(client, config, mode="sync", version="v1.0.0").run()
        self.assertEqual(result.status, "duplicates")
        self.assertEqual(len(result.duplicates), 2)
        self.assertEqual(client.writes(), [])

    def test_publisher_required_for_new_deposit(self):
        client = FakeClient({})
        config = z.Config(
            path="test.json",
            repository=REPOSITORY,
            metadata={
                "title": "Project",
                "creators": [{"person_or_org": {"type": "personal", "name": "Doe"}}],
                "resource_type": {"id": "software"},
                "publication_date": "2026-01-01",
            },
        )
        with self.assertRaises(z.ConfigError) as context:
            self.engine(client, config, mode="sync", version="v1.0.0").run()
        self.assertIn("publisher", str(context.exception))
        self.assertEqual(client.writes(), [])

    def test_create_error_with_orphan_draft_is_cleaned_up(self):
        client = FakeClient({})
        original_post = client.post

        def failing_post(path, body=None):
            if path == "/api/records":
                client.calls.append(("POST", path))
                raise z.ApiError(
                    "metadata.publisher: Missing publisher field",
                    status=201,
                    payload={"id": "999", "is_draft": True},
                )
            return original_post(path, body)

        client.post = failing_post
        config = z.Config(
            path="test.json",
            repository=REPOSITORY,
            metadata={
                "title": "Project",
                "creators": [{"person_or_org": {"type": "personal", "name": "Doe"}}],
                "resource_type": {"id": "software"},
                "publication_date": "2026-01-01",
                "publisher": "Zenodo",
            },
        )
        with self.assertRaises(z.ApiError):
            self.engine(client, config, mode="sync", version="v1.0.0").run()
        self.assertIn(("DELETE", "/api/records/999/draft"), client.calls)

    def test_publish_error_discards_draft(self):
        client = FakeClient({})
        original_post = client.post

        def failing_post(path, body=None):
            if path.endswith("/draft/actions/publish"):
                client.calls.append(("POST", path))
                raise z.ApiError("publish failed", status=500)
            return original_post(path, body)

        client.post = failing_post
        config = z.Config(
            path="test.json",
            repository=REPOSITORY,
            metadata={
                "title": "Project",
                "creators": [{"person_or_org": {"type": "personal", "name": "Doe"}}],
                "resource_type": {"id": "software"},
                "publication_date": "2026-01-01",
                "publisher": "Zenodo",
            },
        )
        with self.assertRaises(z.ApiError):
            self.engine(client, config, mode="sync", version="v1.0.0").run()
        self.assertIn(("DELETE", "/api/records/200/draft"), client.calls)

    def test_anchor_mismatch_refuses_to_write(self):
        record = make_record(custom={"code:codeRepository": "https://github.com/other/repo"})
        client = FakeClient({"100": record})
        config = z.Config(
            path="test.json",
            record="100",
            repository=REPOSITORY,
            metadata={"title": "Project"},
        )
        with self.assertRaises(z.ConfigError):
            self.engine(client, config, mode="sync").run()
        self.assertEqual(client.writes(), [])

    def test_invalid_vocabulary_fails_before_write(self):
        record = make_record()
        client = FakeClient({"100": record})

        class RejectingClient(FakeClient):
            def get(self, path, accept=z.RDM_ACCEPT, retries=2, params=None):
                if path == "/api/vocabularies/code%3AdevelopmentStatus/activ":
                    raise z.ApiError("not found", status=404)
                return super().get(path, accept=accept, retries=retries, params=params)

        rejecting = RejectingClient({"100": record})
        config = z.Config(
            path="test.json",
            record="100",
            custom_fields={"code:developmentStatus": {"id": "activ"}},
        )
        with self.assertRaises(z.ConfigError) as context:
            self.engine(rejecting, config, mode="sync").run()
        self.assertIn("code:developmentStatus", str(context.exception))
        self.assertEqual(rejecting.writes(), [])
        self.assertEqual(client.writes(), [])

    def test_dry_run_writes_nothing(self):
        record = make_record()
        client = FakeClient({"100": record})
        config = z.Config(path="test.json", record="100", metadata={"title": "Other"})
        result = self.engine(client, config, mode="sync", dry_run=True).run()
        self.assertEqual(result.status, "drift")
        self.assertEqual(client.writes(), [])


class SecurityTests(unittest.TestCase):
    def test_redactor_scrubs_token(self):
        redactor = z.Redactor(["s3cret-token"])
        self.assertEqual(redactor("Authorization s3cret-token"), "Authorization ***")

    def test_http_error_scrubs_token(self):
        client = z.HttpClient("https://sandbox.example", token="s3cret-token")
        error = client._http_error(
            "GET", "https://sandbox.example/api", 401, b"s3cret-token is bad"
        )
        self.assertNotIn("s3cret-token", str(error))
        self.assertIn("***", str(error))

    def test_api_error_detail_flattened(self):
        client = z.HttpClient("https://sandbox.example")
        body = json.dumps(
            {
                "message": "Validation error",
                "errors": [
                    {
                        "field": "metadata.version",
                        "messages": ["Not a valid version"],
                    }
                ],
            }
        ).encode()
        error = client._http_error("PUT", "https://sandbox.example/api", 400, body)
        self.assertIn("metadata.version", str(error))
        self.assertIn("Not a valid version", str(error))
        self.assertEqual(error.status, 400)


class SummaryTests(unittest.TestCase):
    def test_drift_table_renders(self):
        record = z.ZenodoRecord(data=make_record(), reason="test")
        result = z.SyncResult(
            status="drift",
            record=record,
            drift=[z.Drift("metadata", "title", "Old", "New")],
            message="test",
        )
        markdown = z.render_summary(result, "check", "https://zenodo.org")
        self.assertIn("`metadata.title`", markdown)
        self.assertIn("https://zenodo.org/records/100", markdown)

    def test_duplicates_include_urls(self):
        result = z.SyncResult(
            status="duplicates",
            record=None,
            duplicates=[make_record(record_id="100"), make_record(record_id="101")],
            message="duplicates",
        )
        markdown = z.render_summary(result, "check", "https://zenodo.org")
        self.assertIn("/records/100", markdown)
        self.assertIn("/records/101", markdown)
        self.assertIn("contact Zenodo support", markdown)


if __name__ == "__main__":
    unittest.main()
