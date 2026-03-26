import inspect
import json
import re
import sys

import webob
import webob.exc

# ---------------------------------------------------------------------------
#  1. The Call Plan (Zero-Reflection Runtime)
# ---------------------------------------------------------------------------


class FunctionCallPlan:
    """
    Pre-analyzes function signatures to avoid 'inspect' during request processing.

    This optimization ensures that we only perform reflection once (at startup)
    rather than on every single incoming HTTP request.
    """
    __slots__ = ('names_required', 'values_default', 'attr_source')

    def __init__(self, fn, attr_source='params'):
        """
        Extracts parameter metadata from a function.

        Args:
            fn: The handler function.
            attr_source: The request attribute to check (e.g., 'params' or 'POST').
        """
        sig = inspect.signature(fn)
        params = [*sig.parameters.values()]

        # Bobo convention: The first parameter is always the request object.
        # We map the remaining parameters to request data.
        self.names_required = [
            param.name for param in params[1:]
            if param.default is inspect.Parameter.empty
            ]
        self.values_default = {
            param.name: param.default
            for param in params[1:]
            if param.default is not inspect.Parameter.empty
            }
        self.attr_source = attr_source


# ---------------------------------------------------------------------------
#  2. Optimized Route & Execution
# ---------------------------------------------------------------------------


class Route:
    """
    Represents a URL route and handles the translation of HTTP requests
    into function calls with automatic argument injection and response generation.
    """
    __slots__ = (
        'handler_function', 'plan', 'regex', 'methods', 'content_type',
        'path'
        )

    def __init__(self, handler_function, prefix=""):
        self.handler_function = handler_function
        self.content_type = getattr(
            handler_function, '_bobo_content_type',
            'text/html; charset=UTF-8'
            )
        self.methods = getattr(handler_function, '_bobo_methods', None)

        path_raw = getattr(
            handler_function, '_bobo_route',
            '/' + handler_function.__name__
            )
        self.path = (prefix.rstrip('/') + '/' +
                     path_raw.lstrip('/')).replace('//', '/')

        self.plan = FunctionCallPlan(
            handler_function,
            getattr(handler_function, '_bobo_params', 'params')
            )
        self.regex = self._compile(self.path)

    def _compile(self, path):
        """Converts Bobo-style paths (/:id) into regex named groups."""
        regex_string = re.sub(
            r'/:([a-zA-Z]\w*)', r'/(?P<\1>[^/]+)', path
            )
        return re.compile(regex_string + '$')

    def handle(self, request_object):
        """
        Matches the request, extracts arguments from JSON/Params, and runs the handler_function.
        """
        # 1. Method and Path Matching
        if self.methods and request_object.method not in self.methods:
            return None

        matcher = self.regex.match(request_object.path_info)
        if matcher is None:
            return None

        # 2. Argument Extraction (Path -> JSON -> Query/Post)
        kwargs = matcher.groupdict()
        source_data = getattr(request_object, self.plan.attr_source)

        # JSON Request Body Handling:
        # If the request is JSON, Bobo maps the top-level keys to function arguments.
        json_data = {}
        if request_object.content_type == 'application/json':
            try:
                # webob.Request.json property parses the body automatically
                json_data = request_object.json
                if not isinstance(json_data, dict):
                    json_data = {}
            except ValueError:
                return webob.exc.HTTPBadRequest(
                    explanation="Malformed JSON body"
                    )

        for name in self.plan.names_required:
            if name in kwargs:
                continue

            # Priority: Path Variables > JSON Keys > Query/POST Params
            value = json_data.get(
                name
                ) if name in json_data else source_data.get(name)

            if value is None:
                return webob.exc.HTTPBadRequest(
                    explanation=f"Missing argument: {name}"
                    )
            kwargs[name] = value

        # 3. Execution
        rv = self.handler_function(request_object, **kwargs)

        # 4. Automatic Response Generation
        if isinstance(rv, webob.Response):
            return rv

        # JSON Response Marshalling
        if 'application/json' in self.content_type:
            try:
                body = json.dumps(rv).encode('utf-8')
                return webob.Response(
                    body=body, content_type=self.content_type
                    )
            except (TypeError, ValueError) as err:
                raise TypeError(
                    f"Failed to marshal JSON response: {err}"
                    )

        # String/Unicode Response Handling
        if isinstance(rv, (str, bytes)):
            if isinstance(rv, bytes):
                return webob.Response(
                    body=rv, content_type=self.content_type
                    )

            charset_match = re.search(
                r'charset=([\w-]+)', self.content_type
                )
            encoding = charset_match.group(
                1
                ) if charset_match else 'utf-8'
            return webob.Response(
                body=rv.encode(encoding),
                content_type=self.content_type
                )

        raise TypeError(
            f"Unsupported return type from handler: {type(rv).__name__}"
            )


# ---------------------------------------------------------------------------
#  3. Decorators (Static Metadata Tagging)
# ---------------------------------------------------------------------------


def _tag(fn, route, methods, content_type, source):
    """Internal helper to attach metadata for the Route class to read later."""
    fn._bobo_route = route
    fn._bobo_methods = methods
    fn._bobo_content_type = content_type
    fn._bobo_params = source
    return fn


def query(route, content_type='text/html; charset=UTF-8'):
    """Handles GET/POST. Defaults to HTML output and 'params' (Query+Post) source."""
    def decorator(fn):
        return _tag(
            fn, route, ('GET', 'POST', 'HEAD'), content_type, 'params'
            )

    return decorator


def post(route, content_type='application/json'):
    """Handles POST only. Defaults to JSON output and 'POST' body source."""
    def decorator(fn):
        return _tag(fn, route, ('POST', ), content_type, 'POST')

    return decorator


# ---------------------------------------------------------------------------
#  4. The Application (Bucket Routing)
# ---------------------------------------------------------------------------


class Application:
    """
    A WSGI application that uses a 'Routing Forest' (buckets) for O(1) lookup.
    """
    def __init__(self, resources=None, prefix=""):
        self.prefix = prefix
        self.buckets = {}
        self.dynamics = []
        for rz in resources or ():
            self.register(rz)

    def register(self, fn):
        """Registers a function by its first path segment."""
        route = Route(fn, prefix=self.prefix)
        seg = route.path.lstrip('/').split('/')[0]
        if seg and not seg.startswith(':'):
            self.buckets.setdefault(seg, []).append(route)
        else:
            self.dynamics.append(route)

    def __call__(self, environ, start_response):
        """WSGI entry point: Finds a matching route and returns its response."""
        request_object = webob.Request(environ)
        seg = request_object.path_info.lstrip('/').split('/', 1)[0]

        # Check segment bucket first, then general dynamic routes
        for route in [*self.buckets.get(seg, []), *self.dynamics]:
            if rsp := route.handle(request_object):
                return rsp(environ, start_response)

        return webob.exc.HTTPNotFound()(environ, start_response)

    @classmethod
    def from_module(cls, name, prefix=""):
        """Factory method to build an app from all tagged functions in a module."""
        module = sys.modules[name]
        handlers = [
            value for value in vars(module).values()
            if hasattr(value, '_bobo_route')
            ]
        return cls(handlers, prefix=prefix)
