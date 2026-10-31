"""Licence interpretation.

The rules here are deliberately conservative: a release is only considered
authorised for automated download when the source explicitly states a licence
or a public-domain status. Anything else is flagged for manual review.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

PUBLIC_DOMAIN_MARKERS = (
    "publicdomain/mark",
    "public domain",
    "public-domain",
    "public_domain",
    "pdm",
)

CC_BY_PREFIX = "creativecommons.org/licenses/by"
CC_ZERO = "creativecommons.org/publicdomain/zero"
CC_MARK = "creativecommons.org/publicdomain/mark"

RESTRICTIVE_MARKERS = (
    "all rights reserved",
    "©",
    "(c)",
    "copyright",
    "unauthorised",
    "unauthorized",
)

TRUSTED_NETLABEL_COLLECTIONS = ("netlabels",)
ARCHIVE_COLLECTIONS_CC = ("audio_music", "opensource_audio", "netlabels")

AUTHORISATION_LABELS = {
    "public_domain": "Public domain",
    "creative_commons": "Creative Commons",
    "official_free": "Officially free download",
    "artist_authorised": "Artist-authorised",
    "label_authorised": "Label-authorised",
    "unknown": "Unverified rights",
}


@dataclass
class LicenceVerdict:
    licence: str
    licence_url: str | None
    authorisation: str
    allows_redistribution: bool
    needs_manual_review: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "licence": self.licence,
            "licence_url": self.licence_url,
            "authorisation": self.authorisation,
            "allows_redistribution": self.allows_redistribution,
            "needs_manual_review": self.needs_manual_review,
            "reason": self.reason,
        }


def _cc_code(url: str) -> tuple[str, bool]:
    lowered = url.lower()
    if CC_ZERO in lowered:
        return "cc0", True
    if CC_MARK in lowered:
        return "public_domain", True
    if CC_BY_PREFIX in lowered:
        if "by-nc-sa" in lowered:
            return "cc_by_nc_sa", True
        if "by-nc-nd" in lowered:
            return "cc_by_nc_nd", False
        if "by-nc" in lowered:
            return "cc_by_nc", True
        if "by-sa" in lowered:
            return "cc_by_sa", True
        if "by-nd" in lowered:
            return "cc_by_nd", False
        return "cc_by", True
    if "sampling" in lowered:
        return "cc_sampling", True
    return "unknown", False


def classify(
    licence_url: str | None = None,
    rights: str | None = None,
    collections: Iterable[str] | None = None,
    declared_by_uploader: bool = True,
) -> LicenceVerdict:
    collections = [str(c).lower() for c in (collections or [])]
    text = " ".join(filter(None, [rights or "", " ".join(collections)])).lower()

    if licence_url:
        code, allows = _cc_code(licence_url)
        if code in {"cc0", "public_domain"}:
            return LicenceVerdict(
                licence=code,
                licence_url=licence_url,
                authorisation="public_domain" if code == "public_domain" else "creative_commons",
                allows_redistribution=True,
                needs_manual_review=False,
                reason=f"Source declares {code} licence",
            )
        if code != "unknown":
            return LicenceVerdict(
                licence=code,
                licence_url=licence_url,
                authorisation="creative_commons",
                allows_redistribution=allows,
                needs_manual_review=not allows,
                reason=f"Source declares Creative Commons licence ({code})",
            )

    if any(marker in text for marker in PUBLIC_DOMAIN_MARKERS):
        return LicenceVerdict(
            licence="public_domain",
            licence_url=licence_url,
            authorisation="public_domain",
            allows_redistribution=True,
            needs_manual_review=False,
            reason="Source states public domain",
        )

    if any(marker in text for marker in RESTRICTIVE_MARKERS):
        return LicenceVerdict(
            licence="all_rights_reserved",
            licence_url=licence_url,
            authorisation="unknown",
            allows_redistribution=False,
            needs_manual_review=True,
            reason="Source indicates rights are reserved; manual verification required",
        )

    if any(collection in TRUSTED_NETLABEL_COLLECTIONS for collection in collections):
        return LicenceVerdict(
            licence="unknown",
            licence_url=licence_url,
            authorisation="unknown",
            allows_redistribution=False,
            needs_manual_review=True,
            reason="Netlabel upload without an explicit licence; verify per release",
        )

    return LicenceVerdict(
        licence="unknown",
        licence_url=licence_url,
        authorisation="unknown",
        allows_redistribution=False,
        needs_manual_review=True,
        reason="No explicit licence found; not eligible for automatic download",
    )


def describe_authorisation(authorisation: str) -> str:
    return AUTHORISATION_LABELS.get(authorisation, authorisation)
