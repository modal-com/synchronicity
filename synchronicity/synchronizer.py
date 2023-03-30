import asyncio
import atexit
import contextlib
import functools
import inspect
import platform
import threading
import typing
import warnings
from typing import Optional

from .async_wrap import type_compat_wraps
from .callback import Callback
from .exceptions import UserCodeException, unwrap_coro_exception, wrap_coro_exception
from .interface import Interface

_BUILTIN_ASYNC_METHODS = {
    "__aiter__": "__iter__",
    "__aenter__": "__enter__",
    "__aexit__": "__exit__",
}

_RETURN_FUTURE_KWARG = "_future"

# Default names for classes
_CLASS_PREFIXES = {
    Interface.BLOCKING: "Blocking",
    Interface.ASYNC: "Async",
}

# Default names for functions
_FUNCTION_PREFIXES = {
    Interface.BLOCKING: "blocking_",
    Interface.ASYNC: "async_",
}

TARGET_INTERFACE_ATTR = "_sync_target_interface"
SYNCHRONIZER_ATTR = "_sync_synchronizer"


def warn_old_modal_client():
    warnings.warn(
        "Using latest synchronicity with an old interface - please upgrade to latest modal-client!"
    )


class Synchronizer:
    """Helps you offer a blocking (synchronous) interface to asynchronous code."""

    def __init__(
        self,
        multiwrap_warning=False,
        async_leakage_warning=True,
    ):
        self._multiwrap_warning = multiwrap_warning
        self._async_leakage_warning = async_leakage_warning
        self._loop = None
        self._loop_creation_lock = threading.Lock()
        self._thread = None
        self._stopping = None

        if platform.system() == "Windows":
            # default event loop policy on windows spits out errors when
            # closing the event loop, so use WindowsSelectorEventLoopPolicy instead
            # https://stackoverflow.com/questions/45600579/asyncio-event-loop-is-closed-when-getting-loop
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

        # Special attribute we use to go from wrapped <-> original
        self._wrapped_attr = "_sync_wrapped_%d" % id(self)
        self._original_attr = "_sync_original_%d" % id(self)

        # Special attribute to mark something as non-wrappable
        self._nowrap_attr = "_sync_nonwrap_%d" % id(self)

        # Prep a synchronized context manager in case one is returned and needs translation
        self._ctx_mgr_cls = contextlib._AsyncGeneratorContextManager
        self.create_async(self._ctx_mgr_cls)
        self.create_blocking(self._ctx_mgr_cls)

        atexit.register(self._close_loop)

    _PICKLE_ATTRS = [
        "_multiwrap_warning",
        "_async_leakage_warning",
    ]

    def __getstate__(self):
        return dict([(attr, getattr(self, attr)) for attr in self._PICKLE_ATTRS])

    def __setstate__(self, d):
        for attr in self._PICKLE_ATTRS:
            setattr(self, attr, d[attr])

    def _start_loop(self):
        with self._loop_creation_lock:
            if self._loop and self._loop.is_running():
                return self._loop

            is_ready = threading.Event()

            def thread_inner():
                async def loop_inner():
                    self._loop = asyncio.get_running_loop()
                    self._stopping = asyncio.Event()
                    is_ready.set()
                    await self._stopping.wait()  # wait until told to stop

                asyncio.run(loop_inner())

            self._thread = threading.Thread(target=thread_inner, daemon=True)
            self._thread.start()
            is_ready.wait()  # TODO: this might block for a very short time
            return self._loop

    def _close_loop(self):
        if self._thread is not None:
            if not self._loop.is_closed():
                # This also serves the purpose of waking up an idle loop
                self._loop.call_soon_threadsafe(self._stopping.set)
            self._thread.join()
            self._thread = None

    def _get_loop(self, start=False):
        if self._loop is None and start:
            return self._start_loop()
        return self._loop

    def _get_running_loop(self):
        # TODO: delete this method
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return

    def _is_inside_loop(self):
        loop = self._get_loop()
        if loop is None:
            return False
        if threading.current_thread() != self._thread:
            # gevent does something bad that causes asyncio.get_running_loop() to return self._loop
            return False
        current_loop = self._get_running_loop()
        return loop == current_loop

    def _wrap_check_async_leakage(self, coro):
        """Check if a coroutine returns another coroutine (or an async generator) and warn.

        The reason this is important to catch is that otherwise even synchronized code might end up
        "leaking" async code into the caller.
        """
        if not self._async_leakage_warning:
            return coro

        async def coro_wrapped():
            value = await coro
            # TODO: we should include the name of the original function here
            if inspect.iscoroutine(value):
                warnings.warn(
                    f"Potential async leakage: coroutine returned a coroutine {value}."
                )
            elif inspect.isasyncgen(value):
                warnings.warn(
                    f"Potential async leakage: Coroutine returned an async generator {value}."
                )
            return value

        return coro_wrapped()

    def _wrap_instance(self, obj, interface):
        # Takes an object and creates a new proxy object for it
        cls = obj.__class__
        cls_dct = cls.__dict__
        interfaces = cls_dct[self._wrapped_attr]
        if interface not in interfaces:
            raise RuntimeError(f"Class {cls} has not synchronized {interface}.")
        interface_cls = interfaces[interface]
        new_obj = interface_cls.__new__(interface_cls)
        # Store a reference to the original object
        new_obj.__dict__[self._original_attr] = obj
        new_obj.__dict__[SYNCHRONIZER_ATTR] = self
        new_obj.__dict__[TARGET_INTERFACE_ATTR] = interface
        return new_obj

    def _translate_scalar_in(self, obj):
        # If it's an external object, translate it to the internal type
        if hasattr(obj, "__dict__"):
            if inspect.isclass(obj):  # TODO: functions?
                return obj.__dict__.get(self._original_attr, obj)
            else:
                return obj.__dict__.get(self._original_attr, obj)
        else:
            return obj

    def _translate_scalar_out(self, obj, interface):
        # If it's an internal object, translate it to the external interface
        if inspect.isclass(obj):  # TODO: functions?
            cls_dct = obj.__dict__
            if self._wrapped_attr in cls_dct:
                return cls_dct[self._wrapped_attr][interface]
            else:
                return obj
        else:
            cls_dct = obj.__class__.__dict__
            if self._wrapped_attr in cls_dct:
                # This is an *instance* of a synchronized class, translate its type
                return self._wrap(obj, interface)
            else:
                return obj

    def _recurse_map(self, mapper, obj):
        if type(obj) == list:
            return list(self._recurse_map(mapper, item) for item in obj)
        elif type(obj) == tuple:
            return tuple(self._recurse_map(mapper, item) for item in obj)
        elif type(obj) == dict:
            return dict(
                (key, self._recurse_map(mapper, item)) for key, item in obj.items()
            )
        else:
            return mapper(obj)

    def _translate_in(self, obj):
        return self._recurse_map(self._translate_scalar_in, obj)

    def _translate_out(self, obj, interface):
        return self._recurse_map(
            lambda scalar: self._translate_scalar_out(scalar, interface), obj
        )

    def _translate_coro_out(self, coro, interface):
        async def unwrap_coro():
            return self._translate_out(await coro, interface)

        return unwrap_coro()

    def _run_function_sync(self, coro, interface):
        if self._is_inside_loop():
            raise Exception(
                "Deadlock detected: calling a sync function from the synchronizer loop"
            )

        coro = wrap_coro_exception(coro)
        coro = self._wrap_check_async_leakage(coro)
        loop = self._get_loop(start=True)
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        value = fut.result()
        return self._translate_out(value, interface)

    def _run_function_sync_future(self, coro, interface):
        coro = wrap_coro_exception(coro)
        coro = self._wrap_check_async_leakage(coro)
        loop = self._get_loop(start=True)
        # For futures, we unwrap the result at this point, not in f_wrapped
        coro = unwrap_coro_exception(coro)
        coro = self._translate_coro_out(coro, interface)
        return asyncio.run_coroutine_threadsafe(coro, loop)

    async def _run_function_async(self, coro, interface):
        coro = wrap_coro_exception(coro)
        coro = self._wrap_check_async_leakage(coro)
        loop = self._get_loop(start=True)
        if self._is_inside_loop():
            value = await coro
        else:
            c_fut = asyncio.run_coroutine_threadsafe(coro, loop)
            a_fut = asyncio.wrap_future(c_fut)
            value = await a_fut
        return self._translate_out(value, interface)

    def _run_generator_sync(self, gen, interface):
        value, is_exc = None, False
        while True:
            try:
                if is_exc:
                    value = self._run_function_sync(gen.athrow(value), interface)
                else:
                    value = self._run_function_sync(gen.asend(value), interface)
            except UserCodeException as uc_exc:
                raise uc_exc.exc from None
            except StopAsyncIteration:
                break
            try:
                value = yield value
                is_exc = False
            except BaseException as exc:
                value = exc
                is_exc = True

    async def _run_generator_async(self, gen, interface):
        value, is_exc = None, False
        while True:
            try:
                if is_exc:
                    value = await self._run_function_async(gen.athrow(value), interface)
                else:
                    value = await self._run_function_async(gen.asend(value), interface)
            except UserCodeException as uc_exc:
                raise uc_exc.exc from None
            except StopAsyncIteration:
                break
            try:
                value = yield value
                is_exc = False
            except BaseException as exc:
                value = exc
                is_exc = True

    def create_callback(self, f, interface):
        return Callback(self, f, interface)

    def _update_wrapper(
        self, f_wrapped, f, name=None, interface=None, target_module=None
    ):
        """Very similar to functools.update_wrapper"""
        functools.update_wrapper(f_wrapped, f)
        if name is not None:
            f_wrapped.__name__ = name
            f_wrapped.__qualname__ = name
        if target_module is not None:
            f_wrapped.__module__ = target_module
        setattr(f_wrapped, SYNCHRONIZER_ATTR, self)
        setattr(f_wrapped, TARGET_INTERFACE_ATTR, interface)

    def _wrap_callable(
        self,
        f,
        interface,
        name=None,
        allow_futures=True,
        unwrap_user_excs=True,
        target_module=None,
    ):
        if hasattr(f, self._original_attr):
            if self._multiwrap_warning:
                warnings.warn(
                    f"Function {f} is already wrapped, but getting wrapped again"
                )
            return f

        if name is None:
            if hasattr(self, "get_name"):
                # super dumb backwards compatibility fix
                warn_old_modal_client()
                name = self.get_name(f, interface)
            else:
                name = _FUNCTION_PREFIXES[interface] + f.__name__

        is_coroutinefunction = inspect.iscoroutinefunction(f)

        @self.wraps_by_interface(interface, f)
        def f_wrapped(*args, **kwargs):
            return_future = kwargs.pop(_RETURN_FUTURE_KWARG, False)

            # If this gets called with an argument that represents an external type,
            # translate it into an internal type
            args = self._translate_in(args)
            kwargs = self._translate_in(kwargs)

            # Call the function
            res = f(*args, **kwargs)

            # Figure out if this is a coroutine or something
            is_coroutine = inspect.iscoroutine(res)
            is_asyncgen = inspect.isasyncgen(res)

            if return_future:
                if not allow_futures:
                    raise Exception("Can not return future for this function")
                elif is_coroutine:
                    return self._run_function_sync_future(res, interface)
                elif is_asyncgen:
                    raise Exception("Can not return futures for generators")
                else:
                    return res
            elif is_coroutine:
                if interface == Interface.ASYNC:
                    coro = self._run_function_async(res, interface)
                    if not is_coroutinefunction:
                        # If this is a non-async function that returns a coroutine,
                        # then this is the exit point, and we need to unwrap any
                        # wrapped exception here. Otherwise, the exit point is
                        # in async_wrap.py
                        coro = unwrap_coro_exception(coro)
                    return coro
                elif interface == Interface.BLOCKING:
                    # This is the exit point, so we need to unwrap the exception here
                    try:
                        return self._run_function_sync(res, interface)
                    except UserCodeException as uc_exc:
                        # Used to skip a frame when called from `proxy_method`.
                        if unwrap_user_excs:
                            raise uc_exc.exc from None
                        else:
                            raise uc_exc
            elif is_asyncgen:
                # Note that the _run_generator_* functions handle their own
                # unwrapping of exceptions (this happens during yielding)
                if interface == Interface.ASYNC:
                    return self._run_generator_async(res, interface)
                elif interface == Interface.BLOCKING:
                    return self._run_generator_sync(res, interface)
            else:
                if inspect.isfunction(res) or isinstance(
                    res, functools.partial
                ):  # TODO: HACKY HACK
                    # TODO: this is needed for decorator wrappers that returns functions
                    # Maybe a bit of a hacky special case that deserves its own decorator
                    @self.wraps_by_interface(interface, res)
                    def f_wrapped(*args, **kwargs):
                        args = self._translate_in(args)
                        kwargs = self._translate_in(kwargs)
                        f_res = res(*args, **kwargs)
                        return self._translate_out(f_res, interface)

                    return f_wrapped

                return self._translate_out(res, interface)

        self._update_wrapper(f_wrapped, f, name, interface, target_module=target_module)
        setattr(f_wrapped, self._original_attr, f)
        return f_wrapped

    def _wrap_proxy_method(synchronizer_self, method, interface, allow_futures=True):
        if getattr(method, synchronizer_self._nowrap_attr, None):
            # This method is marked as non-wrappable
            return method

        method = synchronizer_self._wrap_callable(
            method, interface, allow_futures=allow_futures, unwrap_user_excs=False
        )

        @synchronizer_self.wraps_by_interface(interface, method)
        def proxy_method(self, *args, **kwargs):
            instance = self.__dict__[synchronizer_self._original_attr]
            try:
                return method(instance, *args, **kwargs)
            except UserCodeException as uc_exc:
                raise uc_exc.exc from None

        return proxy_method

    def _wrap_proxy_staticmethod(self, method, interface):
        orig_function = method.__func__
        method = self._wrap_callable(orig_function, interface)
        wrapper = self.wraps_by_interface(interface, orig_function)(method)
        self._update_wrapper(wrapper, orig_function, interface=interface)
        return staticmethod(wrapper)

    def _wrap_proxy_classmethod(self, method, interface):
        method = self._wrap_callable(method.__func__, interface)

        @self.wraps_by_interface(interface, method)
        def proxy_classmethod(cls, *args, **kwargs):
            return method(cls, *args, **kwargs)

        self._update_wrapper(proxy_classmethod, method, interface=interface)
        return classmethod(proxy_classmethod)

    def _wrap_proxy_property(self, prop, interface):
        kwargs = {}
        for attr in ["fget", "fset", "fdel"]:
            if getattr(prop, attr):
                func = getattr(prop, attr)
                kwargs[attr] = self._wrap_proxy_method(func, interface, False)
        return property(**kwargs)

    def _wrap_proxy_constructor(synchronizer_self, cls, interface):
        """Returns a custom __init__ for the subclass."""

        def my_init(self, *args, **kwargs):
            # Create base instance
            args = synchronizer_self._translate_in(args)
            kwargs = synchronizer_self._translate_in(kwargs)
            instance = cls(*args, **kwargs)

            # Register self as the wrapped one
            interface_instances = {interface: self}
            instance.__dict__[synchronizer_self._wrapped_attr] = interface_instances

            # Store a reference to the original object
            self.__dict__[synchronizer_self._original_attr] = instance

        synchronizer_self._update_wrapper(my_init, cls.__init__, interface=interface)
        return my_init

    def _wrap_class(self, cls, interface, name, target_module=None):
        bases = tuple(
            self._wrap(base, interface, require_already_wrapped=(name is not None))
            if base != object
            else object
            for base in cls.__bases__
        )
        new_dict = {self._original_attr: cls}
        if cls is not None:
            new_dict["__init__"] = self._wrap_proxy_constructor(cls, interface)
        for k, v in cls.__dict__.items():
            if k in _BUILTIN_ASYNC_METHODS:
                k_sync = _BUILTIN_ASYNC_METHODS[k]
                if interface == Interface.BLOCKING:
                    new_dict[k_sync] = self._wrap_proxy_method(
                        v, interface, allow_futures=False
                    )
                if interface == Interface.ASYNC:
                    new_dict[k] = self._wrap_proxy_method(
                        v, interface, allow_futures=False
                    )
            elif k in ("__new__", "__init__"):
                # Skip custom constructor in the wrapped class
                # Instead, delegate to the base class constructor and wrap it
                pass
            elif isinstance(v, staticmethod):
                # TODO(erikbern): this feels pretty hacky
                new_dict[k] = self._wrap_proxy_staticmethod(v, interface)
            elif isinstance(v, classmethod):
                new_dict[k] = self._wrap_proxy_classmethod(v, interface)
            elif isinstance(v, property):
                new_dict[k] = self._wrap_proxy_property(v, interface)
            elif callable(v):
                new_dict[k] = self._wrap_proxy_method(v, interface)

        if name is None:
            if hasattr(self, "get_name"):
                # super dumb backwards compatibility fix
                warn_old_modal_client()
                name = self.get_name(cls, interface)
            else:
                name = _CLASS_PREFIXES[interface] + cls.__name__

        new_cls = type.__new__(type, name, bases, new_dict)
        new_cls.__module__ = cls.__module__ if target_module is None else target_module
        new_cls.__doc__ = cls.__doc__
        if "__annotations__" in cls.__dict__:
            new_cls.__annotations__ = cls.__annotations__  # transfer annotations
        setattr(new_cls, TARGET_INTERFACE_ATTR, interface)
        setattr(new_cls, SYNCHRONIZER_ATTR, self)
        return new_cls

    def _wrap(
        self,
        obj,
        interface,
        name=None,
        require_already_wrapped=False,
        target_module=None,
    ):
        # This method works for classes, functions, and instances
        # It wraps the object, and caches the wrapped object

        # Get the list of existing interfaces
        if self._wrapped_attr not in obj.__dict__:
            if isinstance(obj.__dict__, dict):
                # This works for instances
                obj.__dict__.setdefault(self._wrapped_attr, {})
            else:
                # This works for classes & functions
                setattr(obj, self._wrapped_attr, {})

        # If this is already wrapped, return the existing interface
        interfaces = obj.__dict__[self._wrapped_attr]
        if interface in interfaces:
            if self._multiwrap_warning:
                warnings.warn(
                    f"Object {obj} is already wrapped, but getting wrapped again"
                )
            return interfaces[interface]

        if require_already_wrapped:
            # This happens if a class has a custom name but its base class doesn't
            raise RuntimeError(
                f"{obj} needs to be serialized explicitly with a custom name"
            )

        # Wrap object (different cases based on the type)
        if inspect.isclass(obj):
            new_obj = self._wrap_class(
                obj, interface, name, target_module=target_module
            )
        elif inspect.isfunction(obj):
            new_obj = self._wrap_callable(
                obj, interface, name, target_module=target_module
            )
        elif isinstance(obj, typing.TypeVar):
            new_obj = self._wrap_type_var(obj, interface, name, target_module)
        elif self._wrapped_attr in obj.__class__.__dict__:
            new_obj = self._wrap_instance(obj, interface)
        else:
            raise Exception("Argument %s is not a class or a callable" % obj)

        # Store the interface on the obj and return
        interfaces[interface] = new_obj
        return new_obj

    def _wrap_type_var(self, obj, interface, name, target_module):
        # TypeVar translation is needed only for type stub generation, in case the
        # "bound" attribute refers to a translatable type.

        # Creates a new identical TypeVar, marked with synchronicity's special attributes
        # This lets type stubs "translate" the `bounds` attribute on emitted type vars
        # if picked up from module scope and in generics using the base implementation type

        # TODO(elias): Refactor - since this isn't used for live apps, move type stub generation into genstub
        new_obj = typing.TypeVar(name, bound=obj.__bound__)  # noqa
        new_obj.__dict__[self._original_attr] = obj
        new_obj.__dict__[SYNCHRONIZER_ATTR] = self
        new_obj.__dict__[TARGET_INTERFACE_ATTR] = interface
        new_obj.__module__ = target_module
        obj.__dict__.setdefault(self._wrapped_attr, {})
        obj.__dict__[self._wrapped_attr][interface] = new_obj
        return new_obj

    def nowrap(self, obj):
        setattr(obj, self._nowrap_attr, True)
        return obj

    # New interface that (almost) doesn't mutate objects

    def create_blocking(
        self, obj, name: Optional[str] = None, target_module: Optional[str] = None
    ):
        wrapped = self._wrap(obj, Interface.BLOCKING, name, target_module=target_module)
        return wrapped

    def create_async(
        self, obj, name: Optional[str] = None, target_module: Optional[str] = None
    ):
        wrapped = self._wrap(obj, Interface.ASYNC, name, target_module=target_module)
        return wrapped

    def is_synchronized(self, obj):
        if inspect.isclass(obj) or inspect.isfunction(obj):
            return hasattr(obj, self._original_attr)
        else:
            return hasattr(obj.__class__, self._original_attr)

    def wraps_by_interface(self, interface, func):
        return type_compat_wraps(func, interface)

    ### DEPRECATED
    # Only needed because old modal clients don't pin the synchronicity version,
    # so we need this for backwards compatibility for a short while

    def create(self, obj):
        warn_old_modal_client()
        return {
            Interface.ASYNC: self.create_async(obj),
            Interface.BLOCKING: self.create_blocking(obj),
        }

    def __call__(self, obj):
        warn_old_modal_client()
        return self.create_blocking(obj)
