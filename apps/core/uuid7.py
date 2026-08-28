"""UUIDv7 primary keys (RFC 9562).

A UUIDv7 is a 128-bit identifier whose leading 48 bits are a Unix-epoch
millisecond timestamp. That makes it **time-sortable** while still being
opaque: unlike a sequential integer it reveals neither how many records exist
nor what the neighbouring ones are.

    48 bits   Unix epoch milliseconds, big-endian
     4 bits   version (7)
    12 bits   rand_a
     2 bits   variant (0b10)
    62 bits   rand_b

**Why this module exists rather than a dependency or a built-in.** Neither is
available at the versions this template targets: `uuid.uuid7()` is Python 3.14
(we pin 3.12) and `uuidv7()` is PostgreSQL 18 (we run 17.6). Adding a runtime
dependency whose only job is generating a default would be inherited by every
project forged from this one, so ~20 lines are cheaper than the supply chain.

**This is a shim with an end date.** An adopter on Python >= 3.14 can delete it
and use `uuid.uuid7`; one on PostgreSQL >= 18 can use a database-side
`db_default=Func("uuidv7")`. Both are drop-in for new rows.
"""

import secrets
import time
import uuid

__all__ = ["uuid7"]

# Bit positions within the 128-bit integer. Named because the shifts below are
# otherwise unreadable, and an off-by-four here produces a UUID that looks
# entirely plausible while claiming the wrong version.
_VERSION_SHIFT = 76
_VARIANT_SHIFT = 62
_TIMESTAMP_SHIFT = 80


def uuid7() -> uuid.UUID:
    """Return a time-sortable UUIDv7.

    Sortability is to **millisecond** resolution only. Two identifiers created
    within the same millisecond have random relative order — this is not a
    monotonic counter, and must not be used as one. If you need a strict
    ordering, order by a timestamp column and break ties explicitly.

    The random bits come from `secrets` rather than `random`: these values are
    primary keys that may end up in URLs, and a predictable PRNG would make
    them guessable.
    """
    timestamp_ms = time.time_ns() // 1_000_000

    # 74 random bits, taken as 10 bytes; the top 6 of those 80 are overwritten
    # by the version and variant fields below.
    value = (timestamp_ms << _TIMESTAMP_SHIFT) | int.from_bytes(secrets.token_bytes(10), "big")

    # Clear then set, rather than OR alone: the random bits underneath are not
    # zero, so an OR would leave whatever was already there.
    value &= ~(0xF << _VERSION_SHIFT)
    value |= 0x7 << _VERSION_SHIFT

    value &= ~(0b11 << _VARIANT_SHIFT)
    value |= 0b10 << _VARIANT_SHIFT

    return uuid.UUID(int=value)
