"""Carrier sample adapters: uniform access to image channels and audio samples.

A sample space exposes exactly three things to the stego engine:

* ``size`` — how many usable sample slots exist (bits of capacity, 1 bit/slot);
* ``min_value`` / ``max_value`` — the clipping range for LSB matching;
* ``gather`` / ``scatter`` — random access by logical slot index.

Alpha handling: for images only the three colour channels are addressable.
Fully transparent pixels (alpha == 0) are excluded entirely, because many
tools rewrite the RGB value of transparent pixels — hiding data there produces
files that open fine and decode as garbage.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np


@runtime_checkable
class SampleSpace(Protocol):
    size: int
    min_value: int
    max_value: int

    def gather(self, positions: np.ndarray) -> np.ndarray: ...

    def scatter(self, positions: np.ndarray, values: np.ndarray) -> None: ...


class ArraySamples:
    """Linear sample space over a writable 1-D integer array (audio)."""

    __slots__ = ("_flat", "max_value", "min_value", "size")

    def __init__(
        self,
        flat: np.ndarray,
        *,
        min_value: int | None = None,
        max_value: int | None = None,
    ) -> None:
        if flat.ndim != 1:
            raise ValueError("array samples must be 1-D")
        if not np.issubdtype(flat.dtype, np.signedinteger) and not np.issubdtype(
            flat.dtype, np.unsignedinteger
        ):
            raise TypeError("array samples must be integral")
        self._flat = flat
        self.size = int(flat.size)
        info = np.iinfo(flat.dtype)
        self.min_value = int(info.min) if min_value is None else int(min_value)
        self.max_value = int(info.max) if max_value is None else int(max_value)

    def gather(self, positions: np.ndarray) -> np.ndarray:
        return np.asarray(self._flat[positions])

    def scatter(self, positions: np.ndarray, values: np.ndarray) -> None:
        if values.size and (
            int(values.min()) < self.min_value or int(values.max()) > self.max_value
        ):
            raise ValueError("value outside sample range")
        self._flat[positions] = values.astype(self._flat.dtype, copy=False)


class ImageSamples:
    """Channel sample space over a writable ``(h, w, 3|4)`` uint8 array."""

    __slots__ = (
        "_all_active",
        "_channels",
        "_flat",
        "_height",
        "_mask",
        "_prefix",
        "_width",
        "max_value",
        "min_value",
        "size",
        "transparent_pixels",
    )

    def __init__(self, pixels: np.ndarray) -> None:
        if pixels.dtype != np.uint8:
            raise TypeError("image pixels must be uint8")
        if pixels.ndim != 3 or pixels.shape[2] not in (3, 4):
            raise TypeError("expected (h, w, 3) or (h, w, 4) uint8 array")
        if not pixels.flags.writeable:
            raise ValueError("pixel array must be writable")
        self._flat = pixels.reshape(-1)
        self._channels = int(pixels.shape[2])
        self._height = int(pixels.shape[0])
        self._width = int(pixels.shape[1])
        self.min_value = 0
        self.max_value = 255
        self._all_active = True
        self._mask: np.ndarray | None = None
        self._prefix: np.ndarray | None = None
        self.transparent_pixels = 0
        if self._channels == 4:
            alpha = pixels[:, :, 3]
            inactive = int(np.count_nonzero(alpha == 0))
            self.transparent_pixels = inactive
            if inactive:
                self._all_active = False
                self._mask = alpha != 0
                row_counts = self._mask.sum(axis=1, dtype=np.int64)
                self._prefix = np.concatenate(
                    (np.zeros(1, dtype=np.int64), np.cumsum(row_counts))
                )
        if self._all_active:
            self.size = self._height * self._width * 3
        else:
            assert self._prefix is not None
            self.size = 3 * int(self._prefix[-1])

    # -- mapping -----------------------------------------------------------
    def _channel_positions(self, slots: np.ndarray) -> np.ndarray:
        pixel_ordinals = slots // 3
        channel_in_pixel = slots % 3
        if self._all_active:
            direct: np.ndarray = pixel_ordinals * self._channels + channel_in_pixel
            return direct
        assert self._prefix is not None
        assert self._mask is not None
        rows = np.searchsorted(self._prefix, pixel_ordinals, side="right") - 1
        within = pixel_ordinals - self._prefix[rows]
        pixel_indices = np.empty(slots.size, dtype=np.int64)
        order = np.argsort(rows, kind="stable")
        rows_sorted = rows[order]
        unique_rows, starts = np.unique(rows_sorted, return_index=True)
        bounds = np.append(starts, rows_sorted.size)
        for index, row in enumerate(unique_rows):
            segment = order[starts[index] : bounds[index + 1]]
            columns = np.flatnonzero(self._mask[row])
            pixel_indices[segment] = int(row) * self._width + columns[within[segment]]
        mapped: np.ndarray = pixel_indices * self._channels + channel_in_pixel
        return mapped

    # -- sample API --------------------------------------------------------
    def gather(self, positions: np.ndarray) -> np.ndarray:
        return np.asarray(self._flat[self._channel_positions(positions)])

    def gather_pixels(self, slots: np.ndarray) -> np.ndarray:
        """Return the pixel ordinal of each slot (diagnostics/tests)."""
        return np.asarray(slots) // 3

    def scatter(self, positions: np.ndarray, values: np.ndarray) -> None:
        if values.size and (int(values.min()) < 0 or int(values.max()) > 255):
            raise ValueError("pixel value outside 0..255")
        self._flat[self._channel_positions(positions)] = values.astype(np.uint8, copy=False)
