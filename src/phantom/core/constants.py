"""PHANTOM — single source of truth for every security-relevant constant.

Everything the UI or the documentation says about cryptography is derived from
this module (see ``tests/test_claims_match_constants.py``).  If a number is
quoted in a tooltip, a locale string or the README, it must come from here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, unique
from typing import Final

# ---------------------------------------------------------------------------
# Application identity
# ---------------------------------------------------------------------------
APP_NAME: Final = "Phantom"
APP_VERSION: Final = "2.0.0"
APP_STAGE: Final = "release-candidate"
APP_RELEASE_LABEL: Final = f"{APP_NAME} {APP_VERSION}"

# ---------------------------------------------------------------------------
# Container / crypto primitives
# ---------------------------------------------------------------------------
#: Layout written from now on: the syndrome-trellis code of
#: :mod:`phantom.stego.stc`.  Same key-derived header as v3/v4, but the payload is
#: a minimum-cost set of ±1 changes over the whole body instead of an even spread.
#: Nothing about extraction depends on the image, so the layout is exact by
#: construction.  It supersedes v3 as what this build writes.
STC_CONTAINER_VERSION: Final = 5
#: The v3 keyed-positions layout: the v2 AEAD layout with *every* bit of the
#: carrier, header included, at positions derived from the password.  Still the
#: fallback when the trellis cannot be used — audio carriers, carriers with
#: transparent pixels, payloads the plan cannot fit — and still readable.
KEYED_CONTAINER_VERSION: Final = 3
#: Experimental adaptive layout: the header still sits at key-derived positions,
#: but the payload goes to the samples whose ±1 change is least visible, chosen by
#: a Daubechies-8 cost map (:mod:`phantom.stego.adaptive`).  Reachable with
#: ``engine.embed_payload(container_version=C.ADAPTIVE_CONTAINER_VERSION)``; never
#: the default, because selection without syndrome coding is not the final word.
ADAPTIVE_CONTAINER_VERSION: Final = 4
#: Layout written by PHANTOM 2.0.0: public header positions, keyed payload only.
#: Still readable; written only when an older build must open the file.
V2_CONTAINER_VERSION: Final = 2
LEGACY_CONTAINER_VERSION: Final = 1
#: Version this build writes.  v2, v3, v4 and v5 all stay readable.
CONTAINER_VERSION: Final = STC_CONTAINER_VERSION

AES_KEY_BITS: Final = 256
AES_KEY_SIZE: Final = 32
SALT_SIZE: Final = 16
NONCE_SIZE: Final = 12
TAG_SIZE: Final = 16

#: Fixed number of usable carrier bits reserved for the public (password
#: independent) header.  Layout: salt(16) || nonce(12) || encrypted length(22).
HEADER_SALT_SIZE: Final = SALT_SIZE
HEADER_NONCE_SIZE: Final = NONCE_SIZE
LENGTH_PLAINTEXT_SIZE: Final = 6  # version(1) || kdf_profile(1) || payload_len(4)
LENGTH_CIPHERTEXT_SIZE: Final = LENGTH_PLAINTEXT_SIZE + TAG_SIZE
HEADER_BYTES: Final = (
    HEADER_SALT_SIZE + HEADER_NONCE_SIZE + LENGTH_CIPHERTEXT_SIZE
)
HEADER_BITS: Final = HEADER_BYTES * 8

#: Domain separation labels.  Changing any of these breaks every v2 container.
AAD_LABEL: Final = b"PHANTOM/v2"
INFO_LEN_KEY: Final = AAD_LABEL + b"|length-key"
INFO_PAYLOAD_KEY: Final = AAD_LABEL + b"|payload-key"
INFO_PAYLOAD_NONCE: Final = AAD_LABEL + b"|payload-nonce"
INFO_POSITION_KEY: Final = AAD_LABEL + b"|position-key"
#: v2 only: the password-independent permutation that placed the header.  Kept
#: so files written by 2.0.0 still open; the v3 layout never reaches for it.
PUBLIC_POSITION_SEED: Final = b"PHANTOM/v2|public-header-permutation-v1"

#: v3: fixed, public domain-separation salt for the bootstrap key that decides
#: where every bit of the carrier lives.  It has to be fixed: the per-message
#: salt lives inside the header, which cannot be found before the key exists.
POSITION_KDF_SALT: Final = b"PHANTOM/v3|position-bootstrap-salt-v1"
#: HKDF label that splits the bootstrap master key off the message keys.
INFO_POSITION_BOOTSTRAP: Final = b"PHANTOM/v3|position-bootstrap-key"

LEN_AAD: Final = AAD_LABEL + b"|len-aad"
PAYLOAD_AAD_PREFIX: Final = AAD_LABEL + b"|payload-aad"

# ---------------------------------------------------------------------------
# KDF profiles
# ---------------------------------------------------------------------------
ARGON2_MEMORY_KIB: Final = 98_304  # 96 MiB
ARGON2_ITERATIONS: Final = 3
ARGON2_LANES: Final = 1
#: OWASP minimum for Argon2id (m=19 MiB, t=2, p=1).  We are above it.
ARGON2_OWASP_MIN_MEMORY_KIB: Final = 19_456
ARGON2_OWASP_MIN_ITERATIONS: Final = 2
PBKDF2_ITERATIONS: Final = 600_000
PBKDF2_OWASP_MIN_ITERATIONS: Final = 600_000
#: v1 containers were written with this value; kept ONLY for legacy reading.
LEGACY_PBKDF2_ITERATIONS: Final = 200_000

KDF_PROFILE_ARGON2ID: Final = 1
KDF_PROFILE_PBKDF2_SHA256: Final = 2
KDF_PROFILE_LEGACY_V1: Final = 3


@unique
class KdfKind(Enum):
    """Which library primitive implements a profile."""

    ARGON2ID = "argon2id"
    PBKDF2 = "pbkdf2-hmac-sha256"


@dataclass(frozen=True, slots=True)
class KdfProfile:
    """A named, versioned set of KDF parameters."""

    profile_id: int
    label: str
    kind: KdfKind
    memory_kib: int
    iterations: int
    lanes: int

    @property
    def is_fips_compatible(self) -> bool:
        return self.kind is KdfKind.PBKDF2


#: Profiles tried by the extractor, in order.  Legacy v1 is *not* here: it has
#: a completely different on-carrier layout and lives in ``stego.legacy``.
KDF_PROFILES: Final[dict[int, KdfProfile]] = {
    KDF_PROFILE_ARGON2ID: KdfProfile(
        profile_id=KDF_PROFILE_ARGON2ID,
        label="argon2id",
        kind=KdfKind.ARGON2ID,
        memory_kib=ARGON2_MEMORY_KIB,
        iterations=ARGON2_ITERATIONS,
        lanes=ARGON2_LANES,
    ),
    KDF_PROFILE_PBKDF2_SHA256: KdfProfile(
        profile_id=KDF_PROFILE_PBKDF2_SHA256,
        label="pbkdf2-hmac-sha256",
        kind=KdfKind.PBKDF2,
        memory_kib=0,
        iterations=PBKDF2_ITERATIONS,
        lanes=0,
    ),
}
DEFAULT_KDF_PROFILE_ID: Final = KDF_PROFILE_ARGON2ID
#: The bootstrap key that places the header must not depend on the profile the
#: message itself uses (that is negotiated inside the header), so a fixed one.
POSITION_KDF_PROFILE_ID: Final = KDF_PROFILE_PBKDF2_SHA256

# ---------------------------------------------------------------------------
# Password policy
# ---------------------------------------------------------------------------
MIN_PASSWORD_LENGTH: Final = 12
GENERATED_PASSWORD_LENGTH: Final = 20
PASSWORD_NORMALIZATION: Final = "NFKC"  # noqa: S105 - a Unicode form, not a secret
CLIPBOARD_CLEAR_SECONDS: Final = 30

# ---------------------------------------------------------------------------
# Payload model
# ---------------------------------------------------------------------------
TYPE_TEXT: Final = 0x01
TYPE_FILE: Final = 0x02
SHA256_SIZE: Final = 32
MAX_FILENAME_BYTES: Final = 8_192
ZLIB_LEVEL: Final = 9
COMPRESS_CHUNK: Final = 1 << 20
#: Hard ceiling for a decompressed payload — stops zlib bombs (D6).
MAX_PLAINTEXT_BYTES: Final = 1 << 30
#: Metadata bytes a payload always costs: type(1)+name_len(2)+sha(32)+len(4).
PAYLOAD_META_OVERHEAD: Final = 1 + 2 + SHA256_SIZE + 4

# ---------------------------------------------------------------------------
# Steganography engine
# ---------------------------------------------------------------------------
FEISTEL_ROUNDS: Final = 4
#: Capacity heuristics (NOT guarantees) used to colour the fill bar.
CAPACITY_NOTICE_PCT: Final = 70.0
CAPACITY_WARNING_PCT: Final = 90.0
CAPACITY_HEURISTIC_NOTE: Final = "heuristic"
PNG_COMPRESS_LEVEL: Final = 3  # deflate dominates wall-clock: level 6 needs ~2.5x longer
#: Informational: the on-disk price of the faster compression (measured, heuristic).
PNG_LEVEL_3_SIZE_PENALTY_PCT: Final = 12.0
IMAGE_EXTENSIONS: Final[tuple[str, ...]] = (".png",)
AUDIO_WAV_EXTENSIONS: Final[tuple[str, ...]] = (".wav", ".wave")
AUDIO_FLAC_EXTENSIONS: Final[tuple[str, ...]] = (".flac",)
LOSSY_AUDIO_EXTENSIONS: Final[tuple[str, ...]] = (
    ".mp3", ".aac", ".m4a", ".ogg", ".oga", ".opus", ".wma", ".mp2",
)

#: 8-bit PCM WAV is unsigned with a 128 offset; every other width is signed.
WAV_UNSIGNED_8BIT_OFFSET: Final = 128
WAV_SUPPORTED_BITS: Final[tuple[int, ...]] = (8, 16, 24, 32)

# ---------------------------------------------------------------------------
# File handling
# ---------------------------------------------------------------------------
WIPE_CHUNK_BYTES: Final = 1 << 20
WIPE_PASSES: Final = 1  # one CSPRNG pass + fsync + rename + unlink
ATOMIC_TEMP_SUFFIX: Final = ".phantom-tmp"

# ---------------------------------------------------------------------------
# UI / runtime
# ---------------------------------------------------------------------------
WORKER_POLL_MS: Final = 40
#: How often the footer repeats the elapsed time while a job runs.
STAGE_TICK_MS: Final = 1000
ANIMATION_TICK_MS: Final = 16
DEBOUNCE_MS: Final = 320
WATCHDOG_SECONDS: Final = 90.0
SETTINGS_SCHEMA_VERSION: Final = 2


def kdf_summary(profile: KdfProfile) -> str:
    """Human readable KDF parameters, generated from the constants above."""
    if profile.kind is KdfKind.ARGON2ID:
        return (
            f"Argon2id · m={profile.memory_kib // 1024} MiB · "
            f"t={profile.iterations} · p={profile.lanes}"
        )
    return f"PBKDF2-HMAC-SHA256 · {profile.iterations:,} iterations"


def security_summary() -> str:
    """One-line security description used by the About card and the README."""
    return (
        f"AES-256-GCM · {kdf_summary(KDF_PROFILES[DEFAULT_KDF_PROFILE_ID])} · "
        f"SHA-256 · zlib"
    )


def default_kdf_profile() -> KdfProfile:
    return KDF_PROFILES[DEFAULT_KDF_PROFILE_ID]
