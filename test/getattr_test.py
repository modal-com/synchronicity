import asyncio
import pytest
from typing import Dict, Any

from synchronicity import Interface, Synchronizer


def test_getattr():
    s = Synchronizer()

    class Foo:
        _attrs: Dict[str, Any]

        def __init__(self):
            self._attrs = {}

        def __getattr__(self, k):
            if k in self.__annotations__:
                return self.__dict__[k]
            else:
                return self._attrs[k]

        def __setattr__(self, k, v):
            if k in self.__annotations__:
                self.__dict__[k] = v
            else:
                self._attrs[k] = v

    foo = Foo()
    foo.x = 42
    assert foo.x == 42
    with pytest.raises(KeyError):
        foo.y

    BlockingFoo = s.create(Foo)[Interface.BLOCKING]
    assert BlockingFoo.__name__ == "BlockingFoo"

    blocking_foo = BlockingFoo()
    blocking_foo.x = 42
    assert blocking_foo.x == 42
    with pytest.raises(KeyError):
        blocking_foo.y
