from datetime import timedelta
from typing import Any

TLMGR_STATUS_CODES: dict[str, str] = {
    "d": "removed on server",
    "f": "removed locally (force)",
    "u": "update needed",
    "r": "reversed non-update (local newer)",
    "a": "automatically determined needed",
    "i": "will be installed (new)",
    "I": "will be reinstalled",
}


def _parse_tlmgr_timedelta(value: str) -> timedelta | None:
    """Parse a tlmgr ``HH:MM:SS``/``MM:SS`` field into a timedelta.

    Parameters
    ----------
    value : str
        Time field, e.g. ``"00:31"`` or ``"01:02:03"``. ``"-"`` or empty
        means no value.

    Returns
    -------
    datetime.timedelta | None
        Parsed duration, or ``None`` if unset/unparseable.
    """
    if not value or value == "-":
        return None

    parts = value.split(":")
    try:
        parts_int = [int(p) for p in parts]
    except ValueError:
        return None

    match parts_int:
        case [s]:
            return timedelta(seconds=s)
        case [m, s]:
            return timedelta(minutes=m, seconds=s)
        case [h, m, s]:
            return timedelta(hours=h, minutes=m, seconds=s)
        case _:
            return None


def parse_tlmgr_machine_line(line: str) -> dict[str, Any]:
    """Parse a tab-separated machine-readable line from tlmgr.

    Parameters
    ----------
    line : str
        Raw line as emitted by ``tlmgr --machine-readable``.

    Returns
    -------
    dict[str, typing.Any]
        Parsed fields, with ``runtime``/``esttot`` as ``timedelta | None``.
    """
    fields = line.rstrip("\n").split("\t")

    parsed_data: dict[str, Any] = {
        "pkgname": fields[0] if len(fields) > 0 else "",
        "status_code": fields[1] if len(fields) > 1 else "",
        "status_desc": TLMGR_STATUS_CODES.get(fields[1], "unknown")
        if len(fields) > 1
        else "",
        "localrev": fields[2] if len(fields) > 2 else "",
        "serverrev": fields[3] if len(fields) > 3 else "",
        "size_bytes": int(fields[4]) if len(fields) > 4 and fields[4].isdigit() else 0,
        "runtime": _parse_tlmgr_timedelta(fields[5]) if len(fields) > 5 else None,
        "esttot": _parse_tlmgr_timedelta(fields[6]) if len(fields) > 6 else None,
    }

    if len(fields) > 7:
        parsed_data["extra_fields"] = fields[7:]

    return parsed_data


if __name__ == "__main__":
    sample_line = "xunicode\ti\t-\t77682\t25760\t00:31\t00:31\t-\t0.981\t-"

    result = parse_tlmgr_machine_line(sample_line)

    for key, value in result.items():
        print(f"{key + ':':<15} {value}")
