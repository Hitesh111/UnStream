"""Metadata, artwork and classification package."""

from fiiolib.metadata.classification import Classification, classify_genres
from fiiolib.metadata.tagger import TrackMetadata, read_tags, write_tags

__all__ = ["Classification", "TrackMetadata", "classify_genres", "read_tags", "write_tags"]
