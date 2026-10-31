"""Classification, licensing, and duplicate-report tests."""

from __future__ import annotations

from fiiolib.discovery.licences import classify
from fiiolib.metadata.classification import classify_genres


def test_genre_classification_matches_device_folders() -> None:
    assert classify_genres(["Psytrance"]).relative_dir == "Electronic/Psytrance"
    assert classify_genres(["Indian Classical"]).relative_dir == "Indian/Classical"
    assert classify_genres(["Progressive House"]).relative_dir == "Electronic/Progressive-House"
    assert classify_genres(["Soundtrack"]).relative_dir == "Soundtracks/Soundtracks"
    assert classify_genres([]).relative_dir == "Unsorted/Unsorted"
    assert classify_genres(["Noise"]).relative_dir == "Experimental/Experimental"


def test_licence_classification_is_conservative() -> None:
    public_domain = classify(rights="Public domain", collections=["public_domain_music"])
    assert public_domain.allows_redistribution
    assert public_domain.needs_manual_review is False

    cc0 = classify(licence_url="https://creativecommons.org/publicdomain/zero/1.0/")
    assert cc0.licence == "cc0"
    assert cc0.allows_redistribution

    by_nc = classify(licence_url="https://creativecommons.org/licenses/by-nc/4.0/")
    assert by_nc.allows_redistribution is True
    assert by_nc.needs_manual_review is False

    by_nd = classify(licence_url="https://creativecommons.org/licenses/by-nd/4.0/")
    assert by_nd.allows_redistribution is False
    assert by_nd.needs_manual_review is True

    unknown = classify(rights="All rights reserved")
    assert unknown.authorisation == "unknown"
    assert unknown.allows_redistribution is False
    assert unknown.needs_manual_review is True
