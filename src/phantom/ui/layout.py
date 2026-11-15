"""Window geometry math, free of Tk so it can be unit-tested headless.

The expanded result window should cover most of the screen without hiding the
main window underneath it; a fixed pixel size cannot do that across a 13"
laptop and a 27" display, so the size is derived from the screen at run time.
"""

from __future__ import annotations

from typing import Final

#: Fraction of the screen the expanded result window occupies, and its floor.
SCREEN_RATIO: Final = 0.8
MIN_WINDOW: Final = (720, 520)


def window_geometry(
    screen_width: int,
    screen_height: int,
    *,
    ratio: float = SCREEN_RATIO,
    minimum: tuple[int, int] = MIN_WINDOW,
) -> tuple[int, int, int, int]:
    """Centred ``(width, height, x, y)`` for an expanded window.

    Never larger than the screen and never smaller than ``minimum`` (unless the
    screen itself is smaller, in which case the screen wins).
    """
    width = max(minimum[0], int(screen_width * ratio))
    height = max(minimum[1], int(screen_height * ratio))
    width = min(width, screen_width)
    height = min(height, screen_height)
    x = max(0, (screen_width - width) // 2)
    y = max(0, (screen_height - height) // 3)
    return width, height, x, y
