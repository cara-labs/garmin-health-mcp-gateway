"""Original generated FIT fixtures, based on Garmin's public binary protocol.

No athlete data, third-party files, or encoder implementation is copied here.
Definitions are deliberately emitted often to exercise streaming state changes.
"""

import struct
from datetime import UTC, datetime

FIT_EPOCH = datetime(1989, 12, 31, tzinfo=UTC)
BASE_TIME = 1_000_000_000  # Small date_time values are FIT system time, not UTC.


def crc(data: bytes) -> int:
    result = 0
    for byte in data:
        result ^= byte
        for _ in range(8):
            result = (result >> 1) ^ (0xA001 if result & 1 else 0)
    return result


def message(number, fields, *, developer_fields=()):
    """fields: (field number, base type, already encoded bytes)."""
    definition = struct.pack(
        "<BBBHB", 0x60 if developer_fields else 0x40, 0, 0, number, len(fields)
    )
    definition += b"".join(bytes((n, len(value), base)) for n, base, value in fields)
    if developer_fields:
        definition += bytes((len(developer_fields),))
        definition += b"".join(
            bytes((n, len(value), index)) for n, index, value in developer_fields
        )
    payload = b"\x00" + b"".join(value for _, _, value in fields)
    payload += b"".join(value for _, _, value in developer_fields)
    return definition + payload


def fit_file(messages):
    data = b"".join(messages)
    header = struct.pack("<BBHI4s", 14, 0x20, 21171, len(data), b".FIT")
    header += struct.pack("<H", crc(header))
    contents = header + data
    return contents + struct.pack("<H", crc(contents))


def u32(value):
    return struct.pack("<I", value)


def event(timestamp, kind):
    return message(
        21, [(253, 0x86, u32(BASE_TIME + timestamp)), (0, 0, bytes((0,))), (1, 0, bytes((kind,)))]
    )


def record(timestamp=None, *, hr=140, distance=0, speed=0, developer_fields=()):
    fields = [
        (3, 2, bytes((hr,))),
        (5, 0x86, u32(int(distance * 100))),
        (6, 0x84, struct.pack("<H", int(speed * 1000))),
        (250, 0x86, u32(123)),
    ]
    if timestamp is not None:
        fields.insert(0, (253, 0x86, u32(BASE_TIME + timestamp)))
    return message(20, fields, developer_fields=developer_fields)


def developer_definition():
    return [
        message(207, [(3, 2, b"\x00")]),
        message(
            206,
            [
                (0, 2, b"\x00"),
                (1, 2, b"\x01"),
                (2, 2, b"\x84"),
                (3, 7, b"mystery\x00"),
                (8, 7, b"\x00"),
            ],
        ),
    ]
