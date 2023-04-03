"""Utility for monkey patching typing.overload to allow run time retrieval overloads

Requires any @typing.overload to happen within the patched_overload contextmanager, e.g.:

```python
with patched_overload():
    # the following could be imported from some other module (as long as it wasn't already loaded), or inlined:

    @typing.overload
    def foo(a: int) -> float:
        ...

    def foo(a: typing.Union[bool, int]) -> typing.Union[bool, float]:
        if isinstance(a, bool):
            return a
        return float(a)

foo_overloads = get_overloads(foo)  # returns reference to the overloads of foo (the int -> float one in this case) in the order they are declared
"""
import contextlib
import typing
from unittest import mock

overloads = {}
original_overload = typing.overload


def _function_locator(f):
    return (f.__module__, f.__qualname__)


def _tracking_overload(f):
    # hacky thing to track all typing.overload declarations
    global overloads, original_overload
    locator = _function_locator(f)
    overloads.setdefault(locator, []).append(f)
    return original_overload(f)


@contextlib.contextmanager
def patched_overload():
    with mock.patch("typing.overload", _tracking_overload):
        yield


def get_overloads(f):
    return overloads.get(_function_locator(f), [])
