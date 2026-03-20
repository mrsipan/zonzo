##############################################################################
#
# Copyright Zope Foundation and Contributors.
# All Rights Reserved.
#
# This software is subject to the terms of the Zope Public License,
# Version 2.1 (ZPL).  A copy of the ZPL should accompany this distribution.
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND FITNESS
# FOR A PARTICULAR PURPOSE.
#
##############################################################################
"""Create WSGI‑based web applications (no lambda version)."""

__all__ = (
    'Application',
    'early',
    'late',
    'NotFound',
    'order',
    'post',
    'preroute',
    'query',
    'redirect',
    'reroute',
    'resource',
    'resources',
    'scan_class',
    'subroute',
    )

import inspect
import json
import logging
import operator
import re
import sys
import urllib
from functools import lru_cache

import webob

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Exceptions
# ---------------------------------------------------------------------------
class BoboException(Exception):
    """Internal exception that carries a ready‑to‑render response."""

    __slots__ = ('status', 'body', 'content_type', 'headers')

    def __init__(
        self,
        status,
        body,
        content_type='text/html; charset=UTF-8',
        headers=None
        ):
        self.status = status
        self.body = body
        self.content_type = content_type
        self.headers = headers or []


class MissingFormVariable(Exception):
    """Raised when a required form/query variable is missing."""

    __slots__ = ('name', )

    def __init__(self, name):
        self.name = name

    def __str__(self):
        return self.name


class MethodNotAllowed(Exception):
    """Raised when a resource does not support the requested HTTP method."""

    __slots__ = ('allowed', )

    def __init__(self, allowed):
        self.allowed = sorted(allowed)

    def __str__(self):
        return f"Allowed: {', '.join(self.allowed)}"


class NotFound(Exception):
    """Raised when no resource matches the request URL."""


# ---------------------------------------------------------------------------
#  Ordering helpers
# ---------------------------------------------------------------------------
_order_counter = 0
_LATE_BASE = 1 << 99
_EARLY_BASE = -_LATE_BASE


def order():
    """Return an integer that can be used to order resources.

    Each call returns a larger integer than the previous one.
    """
    global _order_counter
    _order_counter += 1
    return _order_counter


def early():
    """Return an order used for resources that should be searched early."""
    return order() + _EARLY_BASE


def late():
    """Return an order used for resources that should be searched late."""
    return order() + _LATE_BASE


# ---------------------------------------------------------------------------
#  Route compiler (regex builder)
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1024)
def _compile_route(route):
    """Compile a route pattern into a regex and a list of named groups.

    Route syntax: /:name? for optional, /:name for required, and optional
    extension like .json after a placeholder.
    """
    if not route.startswith('/'):
        route = '/' + route

    # Split into parts: prefix, placeholder, extension, prefix, ...
    parts = re.split(r'(/:[a-zA-Z]\w*\??)(\.[^/]+)?', route)
    prefix = parts.pop(0)
    regex_parts = []
    group_names = []

    if prefix:
        regex_parts.append(re.escape(prefix))

    while parts:
        placeholder = parts.pop(0)  # e.g. "/:name" or "/:name?"
        extension = parts.pop(0) if parts else None

        optional = placeholder.endswith('?')
        name = placeholder[2:]  # remove "/:"
        if optional:
            name = name[:-1]  # remove trailing "?"
        group_names.append(name)

        # Build regex for this placeholder
        part_re = r'/(?P<%s>[^/]*)' % name
        if optional:
            part_re = '(' + part_re + ')?'
        regex_parts.append(part_re)

        if extension:
            regex_parts.append(re.escape(extension))

        # Next literal string (if any)
        if parts:
            literal = parts.pop(0)
            if literal:
                regex_parts.append(re.escape(literal))

    full_regex = ''.join(regex_parts) + '$'
    return re.compile(full_regex), group_names


# ---------------------------------------------------------------------------
#  Route wrapper that handles parameter injection
# ---------------------------------------------------------------------------
def _make_simple_wrapper(handler, check):
    """Wrap a function that only needs request and route data."""
    def wrapper(request, **route_data):
        if check:
            result = check(None, request, handler)
            if result is not None:
                return result
        return handler(request, **route_data)

    return wrapper


def _make_param_wrapper(handler, check, param_source):
    """Wrap a function that needs form/query/JSON data as keyword arguments."""
    sig = inspect.signature(handler)
    params = list(sig.parameters.values())

    if not params:
        # No parameters at all
        def wrapper(request, **route_data):
            if check:
                result = check(None, request, handler)
                if result is not None:
                    return result
            return handler()

        return wrapper

    # First parameter is expected to be the request object
    # The remaining ones are filled from route_data or form/query/JSON
    param_names = [p.name for p in params[1:]]
    required = set()
    defaults = {}
    for p in params[1:]:
        if p.default is inspect.Parameter.empty:
            required.add(p.name)
        else:
            defaults[p.name] = p.default

    def wrapper(request, **route_data):
        if check:
            result = check(None, request, handler)
            if result is not None:
                return result

        # Start with route_data
        kwargs = {
            name: value
            for name, value in route_data.items() if name in param_names
            }

        # Get the parameter source (request.params or request.POST)
        source = getattr(request, param_source)

        # Cache JSON data if we ever need it
        json_data = None
        for name in param_names:
            if name in kwargs:
                continue

            # Try from request.params / request.POST
            values = source.getall(name)
            if values:
                if len(values) == 1:
                    kwargs[name] = values[0]
                else:
                    kwargs[name] = values
                continue

            # Try from JSON body if content-type is application/json
            if request.content_type == 'application/json':
                if json_data is None:
                    json_data = request.json
                if name in json_data:
                    kwargs[name] = json_data[name]
                    continue

            # If required and still missing, raise an error
            if name in required:
                raise MissingFormVariable(name)

            # Otherwise, the default (from the function signature) will be used

        return handler(request, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
#  Route class – matches a URL path and calls the associated handler
# ---------------------------------------------------------------------------
class Route:
    """A single route that matches a pattern and calls a handler."""

    __slots__ = (
        'pattern', 'regex', 'group_names', 'handler', 'methods',
        'param_source', 'check', 'content_type', '_wrapper'
        )

    def __init__(
        self, pattern, handler, methods, param_source, check,
        content_type
        ):
        self.pattern = pattern
        self.regex, self.group_names = _compile_route(pattern)
        self.handler = handler
        self.methods = methods  # None or frozenset of allowed methods
        self.param_source = param_source
        self.check = check
        self.content_type = content_type

        # Pre‑create the call wrapper (once)
        if param_source:
            self._wrapper = _make_param_wrapper(
                handler, check, param_source
                )
        else:
            self._wrapper = _make_simple_wrapper(handler, check)

    def match_and_handle(self, request, path, method):
        """If the path matches and the method is allowed, return a response."""
        if self.methods and method not in self.methods:
            return None

        match = self.regex.match(path)
        if not match:
            return None

        route_data = {
            k: v
            for k, v in match.groupdict().items() if v is not None
            }
        result = self._wrapper(request, **route_data)

        if hasattr(result, '__call__'):
            return result
        # Wrap non‑response values as a 200 OK response
        return BoboException(200, result, self.content_type)

    def bobo_response(self, request, path, method):
        """Compatibility method used by the application."""
        return self.match_and_handle(request, path, method)


# ---------------------------------------------------------------------------
#  Subroute – matches a prefix and delegates to a nested resource
# ---------------------------------------------------------------------------
class Subroute:
    """A route that extracts a prefix and passes the remaining path to another resource."""

    __slots__ = ('pattern', 'regex', 'group_names', 'resource_factory')

    def __init__(self, pattern, resource_factory):
        self.pattern = pattern
        self.regex, self.group_names = _compile_route(pattern)
        self.resource_factory = resource_factory

    def match_and_handle(self, request, path, method):
        match = self.regex.match(path)
        if not match:
            return None

        route_data = {
            k: v
            for k, v in match.groupdict().items() if v is not None
            }
        remaining = path[len(match.group(0)):]

        resource = self.resource_factory(request, **route_data)
        if resource is None:
            return None

        # The nested resource must have a bobo_response method
        return resource.bobo_response(request, remaining, method)

    def bobo_response(self, request, path, method):
        """Compatibility method."""
        return self.match_and_handle(request, path, method)


# ---------------------------------------------------------------------------
#  Multi-resource (list of resources tried in order)
# ---------------------------------------------------------------------------
class _MultiResource(list):
    """A list of resources that are tried in order until one returns a response."""
    def bobo_response(self, request, path, method):
        for resource in self:
            result = resource(request, path, method)
            if result is not None:
                return result


# ---------------------------------------------------------------------------
#  Decorators
# ---------------------------------------------------------------------------
_DEFAULT_CONTENT_TYPE = 'text/html; charset=UTF-8'


def _set_route_attrs(
    func, route, methods, content_type, check, order_val, param_source
    ):
    """Attach route metadata to a callable."""
    if route is None:
        route = '/' + func.__name__
        # If the content type has a subtype like "application/json", use it as extension
        ext_match = re.search(r'/(\w+)', content_type)
        if ext_match:
            route += '.' + ext_match.group(1)
    func._bobo_route = route
    if methods is not None:
        if isinstance(methods, str):
            methods = (methods, )
        func._bobo_methods = frozenset(methods)
    else:
        func._bobo_methods = None
    func._bobo_content_type = content_type
    func._bobo_check = check
    func._bobo_order = order_val if order_val is not None else order()
    func._bobo_params = param_source
    return func


def resource(
    route=None,
    method=('GET', 'POST', 'HEAD'),
    content_type=_DEFAULT_CONTENT_TYPE,
    check=None,
    order=None
    ):
    """Decorator to define a resource with a route and allowed methods."""
    if callable(route):
        return _set_route_attrs(
            route, None, method, content_type, check, order, None
            )

    def decorator(func):
        return _set_route_attrs(
            func, route, method, content_type, check, order, None
            )

    return decorator


def post(
    route=None,
    content_type=_DEFAULT_CONTENT_TYPE,
    check=None,
    order=None
    ):
    """Decorator that injects POST form data as keyword arguments."""
    if callable(route):
        return _set_route_attrs(
            route, None, ('POST', ), content_type, check, order, 'POST'
            )

    def decorator(func):
        return _set_route_attrs(
            func, route, ('POST', ), content_type, check, order, 'POST'
            )

    return decorator


def query(
    route=None,
    method=('GET', 'POST', 'HEAD'),
    content_type=_DEFAULT_CONTENT_TYPE,
    check=None,
    order=None
    ):
    """Decorator that injects query/form data as keyword arguments."""
    if callable(route):
        return _set_route_attrs(
            route, None, method, content_type, check, order, 'params'
            )

    def decorator(func):
        return _set_route_attrs(
            func, route, method, content_type, check, order, 'params'
            )

    return decorator


def get(
    route, content_type=_DEFAULT_CONTENT_TYPE, check=None, order=None
    ):
    """Shortcut for a GET resource."""
    def decorator(func):
        return _set_route_attrs(
            func, route, ('GET', ), content_type, check, order, 'params'
            )

    return decorator


def head(
    route, content_type=_DEFAULT_CONTENT_TYPE, check=None, order=None
    ):
    """Shortcut for a HEAD resource."""
    def decorator(func):
        return _set_route_attrs(
            func, route, ('HEAD', ), content_type, check, order,
            'params'
            )

    return decorator


def put(
    route, content_type=_DEFAULT_CONTENT_TYPE, check=None, order=None
    ):
    """Shortcut for a PUT resource."""
    def decorator(func):
        return _set_route_attrs(
            func, route, ('PUT', ), content_type, check, order, 'POST'
            )

    return decorator


def delete(
    route, content_type=_DEFAULT_CONTENT_TYPE, check=None, order=None
    ):
    """Shortcut for a DELETE resource."""
    def decorator(func):
        return _set_route_attrs(
            func, route, ('DELETE', ), content_type, check, order, None
            )

    return decorator


def options(
    route, content_type=_DEFAULT_CONTENT_TYPE, check=None, order=None
    ):
    """Shortcut for an OPTIONS resource."""
    def decorator(func):
        return _set_route_attrs(
            func, route, ('OPTIONS', ), content_type, check, order,
            'params'
            )

    return decorator


# ---------------------------------------------------------------------------
#  Subroute and class scanning
# ---------------------------------------------------------------------------
def _subroute(route, obj, scan):
    if scan:
        scan_class(obj)

    if isinstance(obj, type):
        # Create a factory that instantiates the class
        def factory(request, **route_data):
            return obj(request, **route_data)

        return Subroute(route, factory)
    else:
        # obj is already a factory callable
        return Subroute(route, obj)


def subroute(route=None, scan=False, order=None):
    """Decorator to create a nested route (sub‑application)."""
    if callable(route):
        return _subroute('/' + route.__name__, route, scan)

    if isinstance(route, str):

        def wrapper(ob):
            return _subroute(route, ob, scan)

        return wrapper

    # route is None → use decorated object's name
    def wrapper(ob):
        return _subroute('/' + ob.__name__, ob, scan)

    return wrapper


def scan_class(cls):
    """Add a bobo_response method to a class that dispatches to its decorated methods."""
    # Gather all methods with _bobo_route metadata
    route_map = {}
    for base in reversed(inspect.getmro(cls)):
        for name, value in base.__dict__.items():
            if hasattr(value, '_bobo_route'):
                route = value._bobo_route
                route_map.setdefault(route, []).append((name, value))

    # Build a dispatcher for each route
    route_infos = []
    for route, methods in route_map.items():
        # Group by HTTP method
        by_method = {}
        min_order = None
        for name, meth in methods:
            order_val = getattr(meth, '_bobo_order', 0)
            if min_order is None or order_val < min_order:
                min_order = order_val
            method_set = getattr(meth, '_bobo_methods', None)
            if method_set is None:
                by_method[None] = (name, meth)
            else:
                for m in method_set:
                    by_method[m] = (name, meth)

        regex, _ = _compile_route(route)

        # Create a closure that captures the route data
        def route_handler(
            self,
            request,
            path,
            method,
            regex=regex,
            by_method=by_method
            ):
            match = regex.match(path)
            if not match:
                return None
            route_data = {
                k: v
                for k, v in match.groupdict().items() if v is not None
                }
            entry = by_method.get(method)
            if entry is None:
                entry = by_method.get(None)
            if entry is None:
                allowed = set(by_method.keys())
                if None in allowed:
                    allowed.remove(None)
                raise MethodNotAllowed(allowed)
            name, meth = entry
            result = getattr(self, name)(request, **route_data)
            if hasattr(result, '__call__'):
                return result
            content_type = getattr(
                meth, '_bobo_content_type', _DEFAULT_CONTENT_TYPE
                )
            return BoboException(200, result, content_type)

        route_infos.append((min_order, route_handler))

    # Sort by order (lowest first)
    route_infos.sort(key=operator.itemgetter(0))

    # Build the final bobo_response method
    def bobo_response(self, request, path, method):
        for _, handler in route_infos:
            result = handler(self, request, path, method)
            if result is not None:
                return result
        return None

    cls.bobo_response = bobo_response
    return cls


# ---------------------------------------------------------------------------
#  Resource composition helpers
# ---------------------------------------------------------------------------
def reroute(route, resource):
    """Create a new resource by changing the route of an existing resource."""
    if isinstance(resource, str):
        resource = _get_global(resource)
    if hasattr(resource, 'bobo_reroute'):
        return resource.bobo_reroute(route)
    if isinstance(resource, type):
        return Subroute(route, resource)
    raise TypeError("Expected a reroutable resource")


def preroute(route, resource):
    """Prefix a route to an existing resource (or module)."""
    if isinstance(resource, str):
        if ':' in resource:
            resource = _get_global(resource)
        else:
            resource = _MultiResource(_scan_module(resource))
    elif not hasattr(resource, 'bobo_response'):
        resource = _MultiResource(_scan_module(resource.__name__))

    # Create a factory that always returns the same resource
    def factory(_request):
        return resource

    return Subroute(route, factory)


def resources(resources_list):
    """Combine multiple resources into one that tries them in order."""
    handlers = []
    for res in resources_list:
        if isinstance(res, str):
            if ':' in res:
                res = _get_global(res)
            else:
                res = _MultiResource(_scan_module(res))
        elif not hasattr(res, 'bobo_response'):
            res = _MultiResource(_scan_module(res.__name__))
        handlers.append(res.bobo_response)

    def combined_bobo_response(request, path, method):
        for handler in handlers:
            result = handler(request, path, method)
            if result is not None:
                return result
        return None

    class Combined:
        __slots__ = ()
        bobo_response = staticmethod(combined_bobo_response)

    return Combined()


# ---------------------------------------------------------------------------
#  Module scanning and resource collection
# ---------------------------------------------------------------------------
def _import(module_name):
    return __import__(module_name, {}, {}, ['*'])


def _get_global(attr):
    """Resolve a string like 'module:expression' to a Python object."""
    if ':' not in attr:
        raise ValueError("No ':' in global name", attr)
    mod_name, expr = attr.split(':', 1)
    mod = _import(mod_name)
    return eval(expr, mod.__dict__)


def _uncomment(text, split=False):
    lines = [
        line.split('#', 1)[0].strip()
        for line in text.strip().split('\n')
        ]
    lines = [line for line in lines if line]
    if split:
        return lines
    return '\n'.join(lines)


def _create_method_route(route, method_map):
    """Create a route that dispatches to different handlers based on HTTP method."""
    wrappers = {}
    for method, (handler, param_source, check, content_type,
                 _) in method_map.items():
        if param_source:
            wrapper = _make_param_wrapper(handler, check, param_source)
        else:
            wrapper = _make_simple_wrapper(handler, check)
        wrappers[method] = (wrapper, content_type)

    def dispatcher(request, **route_data):
        method = request.method
        entry = wrappers.get(method)
        if entry is None:
            entry = wrappers.get(None)
        if entry is None:
            allowed = set(wrappers.keys())
            if None in allowed:
                allowed.remove(None)
            raise MethodNotAllowed(allowed)
        wrapper, content_type = entry
        result = wrapper(request, **route_data)
        if hasattr(result, '__call__'):
            return result
        return BoboException(200, result, content_type)

    default_content_type = next(iter(method_map.values()))[3]
    return Route(
        route, dispatcher, None, None, None, default_content_type
        )


def _scan_module(module_name):
    """Yield resources (callables with bobo_response) found in a module."""
    mod = _import(module_name)
    # If the module itself has a bobo_response, use it directly
    if hasattr(mod, 'bobo_response'):
        yield mod.bobo_response
        return

    # Collect all objects with _bobo_route metadata
    resources = []
    for obj in mod.__dict__.values():
        if hasattr(obj, '_bobo_route'):
            route = obj._bobo_route
            methods = getattr(obj, '_bobo_methods', None)
            content_type = getattr(
                obj, '_bobo_content_type', _DEFAULT_CONTENT_TYPE
                )
            check = getattr(obj, '_bobo_check', None)
            order_val = getattr(obj, '_bobo_order', 0)
            param_source = getattr(obj, '_bobo_params', None)
            resources.append(
                (
                    order_val, obj, route, methods, param_source, check,
                    content_type
                    )
                )

    # Group by route
    by_route = {}
    for order, handler, route, methods, param_source, check, content_type in resources:
        by_route.setdefault(route, {})[methods] = (
            handler, param_source, check, content_type, order
            )

    # Create Route objects (or method‑dispatched routes)
    for route, method_map in by_route.items():
        if len(method_map) == 1 and None in method_map:
            handler, param_source, check, content_type, _ = method_map[
                None]
            yield Route(
                route, handler, None, param_source, check, content_type
                )
        else:
            yield _create_method_route(route, method_map)


# ---------------------------------------------------------------------------
#  Configuration file parsing
# ---------------------------------------------------------------------------
_resource_re = re.compile(r'\s*([\S]+)\s*([-+]>)\s*(\S+)?\s*$').match


def _route_config(lines):
    """Parse the bobo_resources lines into a list of bobo_response callables."""
    resources = []
    lines = lines[::-1]  # reverse for easy popping
    while lines:
        line = lines.pop()
        m = _resource_re(line)
        if m is None:
            # Just a module or resource name
            route = line
            sep = None
            resource = None
        else:
            route, sep, resource = m.groups()

        if not resource:
            if not sep:
                if ':' in route:
                    resources.append(_get_global(route).bobo_response)
                else:
                    resources.extend(_scan_module(route))
                continue
            else:
                # Continuation line
                resource = lines.pop()

        if sep == '->':
            res = reroute(route, resource)
        else:  # sep == '+>'
            res = preroute(route, resource)
        resources.append(res.bobo_response)

    return resources


# ---------------------------------------------------------------------------
#  Utility functions for responses and redirection
# ---------------------------------------------------------------------------
def _err_response(status, method, title, message, headers=None):
    response = webob.Response(status=status, headerlist=headers or [])
    response.content_type = 'text/html; charset=UTF-8'
    if method != 'HEAD':
        response.unicode_body = f"<html><head><title>{title}</title></head><body>{message}</body></html>"
    return response


def redirect(
    url,
    status=302,
    body=None,
    content_type="text/html; charset=UTF-8"
    ):
    """Return a redirect response."""
    if body is None:
        body = f'See {url}'
    response = webob.Response(
        status=status, headerlist=[('Location', url)]
        )
    response.content_type = content_type
    response.unicode_body = body
    return response


# ---------------------------------------------------------------------------
#  The main WSGI application
# ---------------------------------------------------------------------------
class Application:
    """WSGI application that routes requests to registered resources."""
    def __init__(self, DEFAULT=None, **config):
        if DEFAULT:
            config = dict(DEFAULT, **config)
        self.config = config

        # Run configure callbacks
        bobo_configure = config.get('bobo_configure', '')
        if isinstance(bobo_configure, str):
            for name in filter(
                None,
                _uncomment(bobo_configure).split()
                ):
                configure = _get_global(name)
                configure(config)

        # Set up error handlers
        bobo_errors = config.get('bobo_errors')
        if bobo_errors is not None:
            if isinstance(bobo_errors, str):
                bobo_errors = _uncomment(bobo_errors)
                if ':' in bobo_errors:
                    bobo_errors = _get_global(bobo_errors)
                else:
                    bobo_errors = _import(bobo_errors)
            for attr in (
                'not_found', 'method_not_allowed',
                'missing_form_variable', 'exception'
                ):
                if hasattr(bobo_errors, attr):
                    setattr(self, attr, getattr(bobo_errors, attr))

        # Parse resources
        bobo_resources = config.get('bobo_resources', '')
        if isinstance(bobo_resources, str):
            bobo_resources = _uncomment(bobo_resources, split=True)
            if bobo_resources:
                self.handlers = _route_config(bobo_resources)
            else:
                raise ValueError("Missing bobo_resources option.")
        else:
            self.handlers = [r.bobo_response for r in bobo_resources]

        # Exception handling flag
        handle_exceptions = config.get('bobo_handle_exceptions', True)
        if isinstance(handle_exceptions, str):
            handle_exceptions = handle_exceptions.lower() == 'true'
        self.reraise_exceptions = not handle_exceptions

    def __call__(self, environ, start_response):
        request = webob.Request(environ)
        if request.charset is None:
            request.charset = 'utf8'

        try:
            response = self.bobo_response(
                request, request.path_info, request.method
                )
        except Exception:
            # Let the WSGI server handle it if we are in reraise mode
            if self.reraise_exceptions or environ.get(
                'x-wsgiorg.throw_errors'
                ):
                raise
            return self.exception(
                request, request.method, sys.exc_info()
                )(environ, start_response)

        return response(environ, start_response)

    def bobo_response(self, request, path, method):
        allowed = set()
        for handler in self.handlers:
            try:
                result = handler(request, path, method)
            except MethodNotAllowed as exc:
                allowed.update(exc.allowed)
                continue
            if result is not None:
                if isinstance(result, BoboException):
                    return self.build_response(request, method, result)
                # Already a WSGI response
                return result
        if allowed:
            return self.method_not_allowed(request, method, allowed)
        return self.not_found(request, method)

    def build_response(self, request, method, data):
        """Convert a BoboException into a full WebOb response."""
        response = webob.Response(
            status=data.status, headerlist=data.headers
            )
        response.content_type = data.content_type

        if method == 'HEAD':
            return response

        body = data.body
        if isinstance(body, str):
            response.text = body
        elif isinstance(body, bytes):
            response.body = body
        elif re.match(r'application/json', data.content_type):
            response.body = json.dumps(body).encode('utf-8')
        else:
            raise TypeError(f'Unsupported response type: {type(body)}')
        return response

    # Default error responses
    def not_found(self, request, method):
        return _err_response(
            404, method, "Not Found",
            f"Could not find: {urllib.parse.quote(request.path_info.encode('utf-8'))}"
            )

    def missing_form_variable(self, request, method, name):
        return _err_response(
            403, method, "Missing parameter",
            f'Missing form variable {name}'
            )

    def method_not_allowed(self, request, method, methods):
        return _err_response(
            405, method, "Method Not Allowed",
            f"Invalid request method: {method}",
            [('Allow', ', '.join(sorted(methods)))]
            )

    def exception(self, request, method, exc_info):
        log.exception(request.url)
        return _err_response(
            500, method, "Internal Server Error", "An error occurred."
            )
