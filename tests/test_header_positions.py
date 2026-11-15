"""v3 layout: the password decides where every bit lives, header included.

The v2 layout put the header at constant-seeded positions, so anyone holding
the file knew which bits to read.  These tests pin the replacement: positions
come from a key derived from the password, two passwords never agree on where
the header is, the header never collides with the payload, and files written in
the v2 layout still open.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from phantom.core import constants as C
from phantom.core import container
from phantom.core.errors import WrongPasswordOrNoData
from phantom.core.payload import Payload, pack_payload
from phantom.stego import engine
from phantom.stego.lsb import BitReader, bits_to_bytes
from phantom.stego.permute import keyed_permutation
from phantom.stego.samples import ArraySamples

PASSWORD = "correct horse battery staple"
OTHER_PASSWORD = "another correct horse battery"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
V2_FIXTURES = FIXTURES / "legacy_v2"


def _space(size: int = 60_000, *, seed: int = 7) -> ArraySamples:
    rng = np.random.default_rng(seed)
    return ArraySamples(rng.integers(-4096, 4096, size, dtype=np.int16))


# ---------------------------------------------------------------------------
# positions
# ---------------------------------------------------------------------------
def test_header_positions_depend_on_the_password() -> None:
    """The whole point of v3: a different password, a different header region."""
    space = _space(1_000_000)
    first = engine.header_positions(space, PASSWORD)
    second = engine.header_positions(space, OTHER_PASSWORD)

    assert first.size == C.HEADER_BITS == second.size
    assert not np.array_equal(first, second)
    overlap = np.intersect1d(first, second).size
    assert overlap <= 5, f"{overlap} of {C.HEADER_BITS} header slots coincide"


def test_header_positions_are_deterministic_for_one_password() -> None:
    space = _space()
    first = engine.header_positions(space, PASSWORD)
    second = engine.header_positions(space, PASSWORD)
    np.testing.assert_array_equal(first, second)


def test_header_positions_never_use_the_v2_public_layout() -> None:
    space = _space()
    legacy = engine.legacy_permutation(space).apply(np.arange(C.HEADER_BITS))
    modern = engine.header_positions(space, PASSWORD)
    assert not np.array_equal(legacy, modern)
    assert np.intersect1d(legacy, modern).size < C.HEADER_BITS // 4


def test_header_and_payload_regions_are_disjoint() -> None:
    space = _space()
    bootstrap = engine.bootstrap_for(space, PASSWORD)
    keyed = keyed_permutation(bytes(range(32)), int(space.size) - C.HEADER_BITS)
    header = bootstrap.apply(np.arange(C.HEADER_BITS))
    payload = engine._payload_positions(bootstrap, keyed, 0, 5_000)

    assert header.size == C.HEADER_BITS
    assert payload.size == 5_000
    assert np.intersect1d(header, payload).size == 0
    assert np.unique(payload).size == payload.size


def test_positions_stay_inside_the_carrier() -> None:
    space = _space()
    positions = engine.header_positions(space, PASSWORD)
    assert positions.min() >= 0
    assert positions.max() < int(space.size)


# ---------------------------------------------------------------------------
# end-to-end
# ---------------------------------------------------------------------------
def test_two_passwords_write_two_different_carriers() -> None:
    """The same secret in the same carrier lands in different slots."""
    first, second = _space(), _space()
    blob = pack_payload(
        Payload.text("same secret"), max_payload_bytes=4_000
    ).blob
    engine.embed_payload(first, blob, PASSWORD)
    engine.embed_payload(second, blob, OTHER_PASSWORD)

    header_first = engine.header_positions(first, PASSWORD)
    header_second = engine.header_positions(second, OTHER_PASSWORD)
    assert np.intersect1d(header_first, header_second).size <= 5


def test_a_wrong_password_is_indistinguishable_from_an_empty_carrier() -> None:
    space = _space()
    blob = pack_payload(Payload.text("secret"), max_payload_bytes=4_000).blob
    engine.embed_payload(space, blob, PASSWORD)

    empty = _space(seed=11)
    with pytest.raises(WrongPasswordOrNoData) as wrong:
        engine.extract_payload(space, OTHER_PASSWORD)
    with pytest.raises(WrongPasswordOrNoData) as nothing:
        engine.extract_payload(empty, OTHER_PASSWORD)
    assert type(wrong.value) is type(nothing.value) is WrongPasswordOrNoData


def test_the_header_is_not_where_the_v2_reader_looks() -> None:
    """A v3 carrier must not authenticate through the old public positions."""
    space = _space()
    blob = pack_payload(Payload.text("secret"), max_payload_bytes=4_000).blob
    engine.embed_payload(space, blob, PASSWORD)

    legacy_header = engine.legacy_permutation(space).apply(np.arange(C.HEADER_BITS))
    bits = BitReader(space).read(legacy_header)
    with pytest.raises(WrongPasswordOrNoData):
        container.open_header(
            bits_to_bytes(bits),
            PASSWORD,
            max_payload_len=10_000,
            expected_version=C.V2_CONTAINER_VERSION,
        )


def test_the_bits_at_the_other_password_positions_are_not_the_header() -> None:
    """Reading with the wrong password yields other bits, not the real header."""
    space = _space()
    blob = pack_payload(Payload.text("secret"), max_payload_bytes=4_000).blob
    engine.embed_payload(space, blob, PASSWORD)

    right = BitReader(space).read(engine.header_positions(space, PASSWORD))
    wrong = BitReader(space).read(engine.header_positions(space, OTHER_PASSWORD))
    assert not np.array_equal(right, wrong)
    assert bits_to_bytes(right) != bits_to_bytes(wrong)


@pytest.mark.parametrize("profile_id", sorted(C.KDF_PROFILES))
def test_positions_do_not_depend_on_the_message_kdf_profile(profile_id: int) -> None:
    """The bootstrap key is profile-independent: it has to be found first."""
    space = _space()
    blob = pack_payload(Payload.text("profile independent"), max_payload_bytes=4_000).blob
    engine.embed_payload(
        space, blob, PASSWORD, profile=C.KDF_PROFILES[profile_id]
    )
    extracted = engine.extract_payload(space, PASSWORD)
    assert extracted.data == b"profile independent"


# ---------------------------------------------------------------------------
# backwards compatibility
# ---------------------------------------------------------------------------
def test_v2_layout_round_trips_and_reports_its_version() -> None:
    space = _space()
    blob = pack_payload(Payload.text("written for an older build"), max_payload_bytes=4_000).blob
    engine.embed_payload(
        space, blob, PASSWORD, container_version=C.V2_CONTAINER_VERSION
    )

    extracted = engine.extract_payload(space, PASSWORD)
    assert extracted.data == b"written for an older build"
    assert extracted.container_version == C.V2_CONTAINER_VERSION


def test_v2_carrier_is_found_only_through_the_v2_layout() -> None:
    space = _space()
    blob = pack_payload(Payload.text("public header"), max_payload_bytes=4_000).blob
    engine.embed_payload(
        space, blob, PASSWORD, container_version=C.V2_CONTAINER_VERSION
    )

    legacy = engine.legacy_permutation(space).apply(np.arange(C.HEADER_BITS))
    header = bits_to_bytes(BitReader(space).read(legacy))
    opened = container.open_header(
        header,
        PASSWORD,
        max_payload_len=10_000,
        expected_version=C.V2_CONTAINER_VERSION,
    )
    opened.wipe()
    with pytest.raises(WrongPasswordOrNoData):
        container.open_header(header, PASSWORD, max_payload_len=10_000)


def test_golden_v2_fixture_still_extracts() -> None:
    """A carrier written before v3, pinned by digest, must keep opening."""
    manifest = json.loads((V2_FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    stego = V2_FIXTURES / manifest["text"]["stego"]
    assert hashlib.sha256(stego.read_bytes()).hexdigest() == manifest["text"]["stego_sha256"]

    from phantom.core import api

    outcome = api.extract_from_path(str(stego), manifest["password"])
    assert outcome.payload.container_version == C.V2_CONTAINER_VERSION
    assert outcome.payload.data.decode("utf-8") == manifest["secret_utf8"]
    assert outcome.payload.integrity_ok


def test_golden_v2_fixture_rejects_a_wrong_password() -> None:
    manifest = json.loads((V2_FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    from phantom.core import api

    with pytest.raises(WrongPasswordOrNoData):
        api.extract_from_path(str(V2_FIXTURES / manifest["text"]["stego"]), OTHER_PASSWORD)


def test_new_files_are_written_in_the_current_version(tmp_path: Path) -> None:
    from phantom.core import api

    output = tmp_path / "v3.png"
    carrier = V2_FIXTURES / "carrier.png"
    api.embed_payload(
        str(carrier), str(output), Payload.text("fresh"), password=PASSWORD
    )
    outcome = api.extract_from_path(str(output), PASSWORD)
    assert outcome.payload.container_version == C.CONTAINER_VERSION
    assert outcome.payload.data == b"fresh"
