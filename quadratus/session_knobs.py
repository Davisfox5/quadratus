"""Session settings a caller may choose from outside the engine.

Each name is a ``SessionConfig`` field that had no flag and no form field,
so its default was the only value a run could ever have. A caller passes the
ones it wants changed; a name left out keeps ``SessionConfig``'s own default,
so a run that sets none is the run made before this table existed.

One table for every entry (the command line today, the Project form when it
grows the fields), so a setting is accepted or refused the same way from
either, and nothing outside this list reaches the config by this route.
"""

from __future__ import annotations

from typing import Mapping, Optional

__all__ = ["SESSION_KNOBS", "session_options"]

#: Setting name -> the smallest whole number accepted, or None for an on/off
#: switch. Each floor is the smallest value the engine honours as written:
#: ``recent_entries`` of 0 would slice the whole ledger back in; the first
#: revision always runs, so ``max_fix_cycles`` of 0 would behave as 1; the
#: lead prompt offers CONSULT with the number in it, so ``max_consults`` of 0
#: would offer a channel every use of which fails the task; and
#: ``max_parallel_tasks`` of 1 is how parallel batches are turned off.
SESSION_KNOBS = {
    "recent_entries": 1,
    "max_fix_cycles": 1,
    "max_consults": 1,
    "max_parallel_tasks": 1,
    "spread_leads": None,
    "design_cross_check": None,
}


def session_options(raw: Optional[Mapping]) -> dict:
    """``raw`` checked against :data:`SESSION_KNOBS`, as a plain dict.

    Raises ValueError naming the setting for a name outside the table, a
    switch that is not a bool, or a count that is not a whole number at or
    above its floor. Nothing is coerced: a caller that parses text does its
    own parsing and hands over real values.
    """
    checked = {}
    for name, value in dict(raw or {}).items():
        if name not in SESSION_KNOBS:
            raise ValueError(
                f"{name!r} is not a session setting that can be chosen here; "
                f"the settings are {', '.join(sorted(SESSION_KNOBS))}"
            )
        floor = SESSION_KNOBS[name]
        if floor is None:
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be True or False, not {value!r}")
        elif isinstance(value, bool) or not isinstance(value, int) or value < floor:
            raise ValueError(f"{name} must be a whole number of at least {floor}, not {value!r}")
        checked[name] = value
    return checked
