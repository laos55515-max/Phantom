"""Steganography engines: sample spaces, keyed permutations, image carriers."""

from __future__ import annotations

from phantom.stego.engine import (
    Capacity,
    EmbedReport,
    capacity_for,
    embed_payload,
    extract_payload,
)
from phantom.stego.image import (
    ImageEmbedResult,
    ImageInfo,
    extract_from_image,
    image_capacity,
    probe_image,
)
from phantom.stego.samples import ArraySamples, ImageSamples, SampleSpace

__all__ = [
    "ArraySamples",
    "Capacity",
    "EmbedReport",
    "ImageEmbedResult",
    "ImageInfo",
    "ImageSamples",
    "SampleSpace",
    "capacity_for",
    "embed_payload",
    "extract_from_image",
    "extract_payload",
    "image_capacity",
    "probe_image",
]
