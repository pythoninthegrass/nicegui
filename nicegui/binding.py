from __future__ import annotations

import asyncio
import copyreg
import dataclasses
import time
import weakref
from collections import defaultdict
from contextvars import ContextVar
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    DefaultDict,
    Iterable,
    List,
    Mapping,
    Optional,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
)

from typing_extensions import dataclass_transform

from . import core
from .logging import log

if TYPE_CHECKING:
    from _typeshed import DataclassInstance, IdentityFunction

MAX_PROPAGATION_TIME = 0.01

propagation_visited: ContextVar[Optional[Set[Tuple[int, str]]]] = ContextVar('propagation_visited', default=None)

bindings: DefaultDict[Tuple[int, str], List[Tuple[Any, Any, str, Optional[Callable[[Any], Any]]]]] = defaultdict(list)
bindable_properties: weakref.WeakValueDictionary[Tuple[int, str], Any] = weakref.WeakValueDictionary()
active_links: List[Tuple[Any, str, Any, str, Optional[Callable[[Any], Any]]]] = []

TC = TypeVar('TC', bound=type)
T = TypeVar('T')


def _has_attribute(obj: Union[object, Mapping], name: str) -> Any:
    if isinstance(obj, Mapping):
        return name in obj
    return hasattr(obj, name)


def _get_attribute(obj: Union[object, Mapping], name: str) -> Any:
    if isinstance(obj, Mapping):
        return obj[name]
    return getattr(obj, name)


def _set_attribute(obj: Union[object, Mapping], name: str, value: Any) -> None:
    if isinstance(obj, dict):
        obj[name] = value
    else:
        setattr(obj, name, value)


async def refresh_loop() -> None:
    """Refresh all bindings in an endless loop."""
    while True:
        _refresh_step()
        try:
            await asyncio.sleep(core.app.config.binding_refresh_interval)
        except asyncio.CancelledError:
            break


def _refresh_step() -> None:
    t = time.time()
    for link in active_links:
        (source_obj, source_name, target_obj, target_name, transform) = link
        if _has_attribute(source_obj, source_name):
            source_value = _get_attribute(source_obj, source_name)
            value = transform(source_value) if transform else source_value
            if not _has_attribute(target_obj, target_name) or _get_attribute(target_obj, target_name) != value:
                _set_attribute(target_obj, target_name, value)
                _propagate(target_obj, target_name)
        del link, source_obj, target_obj  # pylint: disable=modified-iterating-list
    if time.time() - t > MAX_PROPAGATION_TIME:
        log.warning(f'binding propagation for {len(active_links)} active links took {time.time() - t:.3f} s')


def _propagate(source_obj: Any, source_name: str) -> None:
    token = propagation_visited.set(set())
    try:
        _propagate_recursively(source_obj, source_name)
    finally:
        propagation_visited.reset(token)


def _propagate_recursively(source_obj: Any, source_name: str) -> None:
    visited = propagation_visited.get()
    assert visited is not None, 'propagation_visited is not set'

    source_obj_id = id(source_obj)
    if (source_obj_id, source_name) in visited:
        return
    visited.add((source_obj_id, source_name))

    if not _has_attribute(source_obj, source_name):
        return
    source_value = _get_attribute(source_obj, source_name)

    for _, target_obj, target_name, transform in bindings.get((source_obj_id, source_name), []):
        if (id(target_obj), target_name) in visited:
            continue

        target_value = transform(source_value) if transform else source_value
        if not _has_attribute(target_obj, target_name) or _get_attribute(target_obj, target_name) != target_value:
            _set_attribute(target_obj, target_name, target_value)
            _propagate_recursively(target_obj, target_name)


def bind_to(self_obj: Any, self_name: str, other_obj: Any, other_name: str,
            forward: Optional[Callable[[Any], Any]] = None) -> None:
    """Bind the property of one object to the property of another object.

    The binding works one way only, from the first object to the second.
    The update happens immediately and whenever a value changes.

    :param self_obj: The object to bind from.
    :param self_name: The name of the property to bind from.
    :param other_obj: The object to bind to.
    :param other_name: The name of the property to bind to.
    :param forward: A function to apply to the value before applying it (default: identity).
    """
    bindings[(id(self_obj), self_name)].append((self_obj, other_obj, other_name, forward))
    if (id(self_obj), self_name) not in bindable_properties:
        active_links.append((self_obj, self_name, other_obj, other_name, forward))
    _propagate(self_obj, self_name)


def bind_from(self_obj: Any, self_name: str, other_obj: Any, other_name: str,
              backward: Optional[Callable[[Any], Any]] = None) -> None:
    """Bind the property of one object from the property of another object.

    The binding works one way only, from the second object to the first.
    The update happens immediately and whenever a value changes.

    :param self_obj: The object to bind to.
    :param self_name: The name of the property to bind to.
    :param other_obj: The object to bind from.
    :param other_name: The name of the property to bind from.
    :param backward: A function to apply to the value before applying it (default: identity).
    """
    bindings[(id(other_obj), other_name)].append((other_obj, self_obj, self_name, backward))
    if (id(other_obj), other_name) not in bindable_properties:
        active_links.append((other_obj, other_name, self_obj, self_name, backward))
    _propagate(other_obj, other_name)


def bind(self_obj: Any, self_name: str, other_obj: Any, other_name: str, *,
         forward: Optional[Callable[[Any], Any]] = None,
         backward: Optional[Callable[[Any], Any]] = None) -> None:
    """Bind the property of one object to the property of another object.

    The binding works both ways, from the first object to the second and from the second to the first.
    The update happens immediately and whenever a value changes.
    The backward binding takes precedence for the initial synchronization.

    :param self_obj: First object to bind.
    :param self_name: The name of the first property to bind.
    :param other_obj: The second object to bind.
    :param other_name: The name of the second property to bind.
    :param forward: A function to apply to the value before applying it to the second object (default: identity).
    :param backward: A function to apply to the value before applying it to the first object (default: identity).
    """
    bind_from(self_obj, self_name, other_obj, other_name, backward=backward)
    bind_to(self_obj, self_name, other_obj, other_name, forward=forward)


class BindableProperty:

    def __init__(self, on_change: Optional[Callable[..., Any]] = None) -> None:
        self._change_handler = on_change

    def __set_name__(self, _, name: str) -> None:
        self.name = name  # pylint: disable=attribute-defined-outside-init

    def __get__(self, owner: Any, _=None) -> Any:
        return getattr(owner, '___' + self.name)

    def __set__(self, owner: Any, value: Any) -> None:
        has_attr = hasattr(owner, '___' + self.name)
        if not has_attr:
            _make_copyable(type(owner))
        value_changed = has_attr and getattr(owner, '___' + self.name) != value
        if has_attr and not value_changed:
            return
        setattr(owner, '___' + self.name, value)
        key = (id(owner), str(self.name))
        bindable_properties[key] = owner
        _propagate(owner, self.name)
        if value_changed and self._change_handler is not None:
            self._change_handler(owner, value)


def remove(objects: Iterable[Any]) -> None:
    """Remove all bindings that involve the given objects.

    :param objects: The objects to remove.
    """
    object_ids = set(map(id, objects))
    active_links[:] = [
        (source_obj, source_name, target_obj, target_name, transform)
        for source_obj, source_name, target_obj, target_name, transform in active_links
        if id(source_obj) not in object_ids and id(target_obj) not in object_ids
    ]
    for key, binding_list in list(bindings.items()):
        binding_list[:] = [
            (source_obj, target_obj, target_name, transform)
            for source_obj, target_obj, target_name, transform in binding_list
            if id(source_obj) not in object_ids and id(target_obj) not in object_ids
        ]
        if not binding_list:
            del bindings[key]
    for obj_id, name in list(bindable_properties):
        if obj_id in object_ids:
            del bindable_properties[(obj_id, name)]


def reset() -> None:
    """Clear all bindings.

    This function is intended for testing purposes only.
    """
    bindings.clear()
    bindable_properties.clear()
    active_links.clear()


@dataclass_transform()
def bindable_dataclass(cls: Optional[TC] = None, /, *,
                       bindable_fields: Optional[Iterable[str]] = None,
                       **kwargs: Any) -> Union[Type[DataclassInstance], IdentityFunction]:
    """A decorator that transforms a class into a dataclass with bindable fields.

    This decorator extends the functionality of ``dataclasses.dataclass`` by making specified fields bindable.
    If ``bindable_fields`` is provided, only the listed fields are made bindable.
    Otherwise, all fields are made bindable by default.

    *Added in version 2.11.0*

    :param cls: class to be transformed into a dataclass
    :param bindable_fields: optional list of field names to make bindable (defaults to all fields)
    :param kwargs: optional keyword arguments to be forwarded to ``dataclasses.dataclass``.
    Usage of ``slots=True`` and ``frozen=True`` are not supported and will raise a ValueError.

    :return: resulting dataclass type
    """
    if cls is None:
        def wrap(cls_):
            return bindable_dataclass(cls_, bindable_fields=bindable_fields, **kwargs)
        return wrap

    for unsupported_option in ('slots', 'frozen'):
        if kwargs.get(unsupported_option):
            raise ValueError(f'`{unsupported_option}=True` is not supported with bindable_dataclass')

    dataclass: Type[DataclassInstance] = dataclasses.dataclass(**kwargs)(cls)
    field_names = set(field.name for field in dataclasses.fields(dataclass))
    if bindable_fields is None:
        bindable_fields = field_names
    for field_name in bindable_fields:
        if field_name not in field_names:
            raise ValueError(f'"{field_name}" is not a dataclass field')
        bindable_property = BindableProperty()
        bindable_property.__set_name__(dataclass, field_name)
        setattr(dataclass, field_name, bindable_property)
    return dataclass


def _make_copyable(cls: Type[T]) -> None:
    """Tell the copy module to update the ``bindable_properties`` dictionary when an object is copied."""
    if cls in copyreg.dispatch_table:
        return

    def _pickle_function(obj: T) -> Tuple[Callable[..., T], Tuple[Any, ...]]:
        reduced = obj.__reduce__()
        assert isinstance(reduced, tuple)
        creator = reduced[0]

        def creator_with_hook(*args, **kwargs) -> T:
            copy = creator(*args, **kwargs)
            for attr_name in dir(obj):
                if (id(obj), attr_name) in bindable_properties:
                    bindable_properties[(id(copy), attr_name)] = copy
            return copy
        return (creator_with_hook, *reduced[1:])
    copyreg.pickle(cls, _pickle_function)
