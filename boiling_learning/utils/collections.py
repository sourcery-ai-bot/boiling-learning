from typing import (
    Callable,
    Dict,
    Generic,
    Hashable,
    Iterable,
    Iterator,
    KeysView,
    MutableSet,
    TypeVar,
    ValuesView,
)

_Key = TypeVar('_Key', bound=Hashable)
_Value = TypeVar('_Value')


class KeyedSet(MutableSet[_Value], Generic[_Key, _Value]):
    def __init__(
        self, key: Callable[[_Value], _Key], iterable: Iterable[_Value] = ()
    ) -> None:
        self.__key: Callable[[_Value], _Key] = key
        self.__data: Dict[_Key, _Value] = {
            self.__key(element): element for element in iterable
        }

    # ------------------------------------------------------------------------
    # Set-like operations
    # ------------------------------------------------------------------------
    def __len__(self) -> int:
        return len(self.__data)

    def __contains__(self, element: _Value) -> bool:
        return element in self.values()

    def __iter__(self) -> Iterator[_Value]:
        return iter(self.values())

    def add(self, element: _Value) -> None:
        self.__data[self.__key(element)] = element

    def remove(self, element: _Value) -> None:
        del self[self.__key(element)]

    def discard(self, element: _Value) -> None:
        try:
            self.remove(element)
        except KeyError:
            pass

    # TODO: implement other operations as in the standard docs:
    # <https://docs.python.org/3/library/stdtypes.html#set-types-set-frozenset>

    # ------------------------------------------------------------------------
    # Dict-like operations
    # ------------------------------------------------------------------------
    def __getitem__(self, key: _Key) -> _Value:
        return self.__data[key]

    def __delitem__(self, key: _Key) -> None:
        del self.__data[key]

    def keys(self) -> KeysView:
        return self.__data.keys()

    def values(self) -> ValuesView:
        return self.__data.values()
