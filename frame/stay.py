"""Who is staying, as the hub reports it.

The only place Frame asks. Today the hub gets it from the Stays app; if that
moves, only the hub changes.
"""
from dataclasses import dataclass
from datetime import datetime


class Unavailable(Exception):
    """The hub couldn't say who is staying (keep the last known state)."""


@dataclass(frozen=True)
class Current:
    key: str
    guest: str
    checkout: datetime | None
    owner: bool


def current():
    """The stay in progress, or None. Raises :class:`Unavailable`."""
    from bdh_sdk import stays

    try:
        found = stays.current()
    except Exception as exc:  # RPC errors, a hub that is restarting, ...
        raise Unavailable(str(exc)) from exc
    if found is None:
        return None
    stay = found.stay
    return Current(key=found.key, guest=stay.guest or "", checkout=stay.checkout, owner=bool(stay.owner))
