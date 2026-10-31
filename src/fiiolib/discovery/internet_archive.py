"""Internet Archive search and item inspection (authorised/CC/public-domain audio)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from fiiolib.discovery.http import PoliteClient
from fiiolib.discovery.licences import LicenceVerdict, classify

logger = logging.getLogger(__name__)

SEARCH_URL = "https://archive.org/advancedsearch.php"
METADATA_URL = "https://archive.org/metadata"
DOWNLOAD_URL = "https://archive.org/download"

AUDIO_EXTENSIONS = (".flac", ".mp3", ".ogg", ".m4a", ".wav", ".ape")


@dataclass
class ArchiveItem:
    identifier: str
    title: str
    creator: str | None
    year: str | None
    collections: list[str] = field(default_factory=list)
    licence_url: str | None = None
    rights: str | None = None
    downloads: int = 0
    files: list[dict[str, Any]] = field(default_factory=list)
    verdict: LicenceVerdict | None = None

    @property
    def torrent_url(self) -> str:
        return f"{DOWNLOAD_URL}/{self.identifier}/{self.identifier}_archive.torrent"

    @property
    def page_url(self) -> str:
        return f"https://archive.org/details/{self.identifier}"

    def audio_files(self) -> list[dict[str, Any]]:
        return [
            f
            for f in self.files
            if str(f.get("name", "")).lower().endswith(AUDIO_EXTENSIONS)
        ]

    def best_format(self) -> tuple[str | None, str | None]:
        files = self.audio_files()
        if any(str(f["name"]).lower().endswith(".flac") for f in files):
            return "flac", "lossless"
        if any(str(f["name"]).lower().endswith(".wav") for f in files):
            return "wav", "lossless"
        if any(str(f["name"]).lower().endswith(".mp3") for f in files):
            return "mp3", "lossy"
        for f in files:
            ext = str(f["name"]).rsplit(".", 1)[-1].lower()
            return ext, "unknown"
        return None, None

    def total_audio_bytes(self) -> int:
        total = 0
        for f in self.audio_files():
            try:
                total += int(f.get("size", 0) or 0)
            except (TypeError, ValueError):
                continue
        return total


class InternetArchiveClient:
    def __init__(self, user_agent: str, min_interval: float = 1.0, cache_dir=None) -> None:
        self.http = PoliteClient(user_agent, min_interval=min_interval, cache_dir=cache_dir)

    def close(self) -> None:
        self.http.close()

    def search(self, query: str, rows: int = 25) -> list[dict[str, Any]]:
        params: list[tuple[str, Any]] = [
            ("q", query),
            ("fl[]", "identifier"),
            ("fl[]", "title"),
            ("fl[]", "creator"),
            ("fl[]", "year"),
            ("fl[]", "collection"),
            ("fl[]", "licenseurl"),
            ("fl[]", "rights"),
            ("fl[]", "downloads"),
            ("fl[]", "mediatype"),
            ("rows", rows),
            ("page", 1),
            ("output", "json"),
            ("sort[]", "downloads desc"),
        ]
        payload = self.http.get_json(
            SEARCH_URL,
            params=params,
            cache_key=f"ia_search_{query}_{rows}",
            max_age_seconds=24 * 3600,
        )
        docs = payload.get("response", {}).get("docs", [])
        return docs

    def search_audio_by_creator(self, creator: str, rows: int = 25) -> list[dict[str, Any]]:
        query = f'creator:("{_escape(creator)}") AND mediatype:(audio)'
        return self.search(query, rows=rows)

    def search_audio_by_title(self, title: str, rows: int = 10) -> list[dict[str, Any]]:
        query = f'title:("{_escape(title)}") AND mediatype:(audio)'
        return self.search(query, rows=rows)

    def item(self, identifier: str) -> ArchiveItem | None:
        payload = self.http.get_json(
            f"{METADATA_URL}/{identifier}", cache_key=f"ia_item_{identifier}", max_age_seconds=7 * 24 * 3600
        )
        if not payload:
            return None
        metadata = payload.get("metadata") or {}
        collections = metadata.get("collection") or []
        if isinstance(collections, str):
            collections = [collections]
        licence_url = _first(metadata.get("licenseurl"))
        rights = _first(metadata.get("rights"))
        item = ArchiveItem(
            identifier=identifier,
            title=_first(metadata.get("title")) or identifier,
            creator=_first(metadata.get("creator")),
            year=_first(metadata.get("year")) or _first(metadata.get("date")),
            collections=[str(c) for c in collections],
            licence_url=licence_url,
            rights=rights,
            downloads=int(_first(metadata.get("downloads")) or 0),
            files=payload.get("files") or [],
        )
        item.verdict = classify(
            licence_url=licence_url,
            rights=rights,
            collections=item.collections,
        )
        return item

    def items_from_docs(self, docs: list[dict[str, Any]]) -> list[ArchiveItem]:
        items = []
        for doc in docs:
            collections = doc.get("collection") or []
            if isinstance(collections, str):
                collections = [collections]
            licence_url = _first(doc.get("licenseurl"))
            rights = _first(doc.get("rights"))
            item = ArchiveItem(
                identifier=doc.get("identifier", ""),
                title=_first(doc.get("title")) or doc.get("identifier", ""),
                creator=_first(doc.get("creator")),
                year=_first(doc.get("year")),
                collections=[str(c) for c in collections],
                licence_url=licence_url,
                rights=rights,
                downloads=int(doc.get("downloads") or 0),
            )
            item.verdict = classify(
                licence_url=licence_url, rights=rights, collections=collections
            )
            items.append(item)
        return items


def _first(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _escape(value: str) -> str:
    return value.replace('"', '\\"')
