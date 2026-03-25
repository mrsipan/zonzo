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

    Attributes:
        names_required (list): Arguments that must be provided from request data.
        values_default (dict): Mapping of argument names to their default values.
        attr_source (str): The WebOb request attribute (params, POST, etc.) to use.
    """
    __slots__ = ('names_required', 'values_default', 'attr_source')

    def __init__(self, fn, attr_source='params'):
        sig = inspect.signature(fn)
        params = [*sig.parameters.values()]

        # Assume first param is 'request_object', map the rest
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
    Handles URL matching and automatic response generation based on return types.
    """
    __slots__ = (
        'handler', 'plan', 'regex', 'methods', 'content_type', 'path'
        )

    def __init__(self, handler, prefix=""):
        self.handler = handler
        self.content_type = getattr(
            handler, '_bobo_content_type', 'text/html; charset=UTF-8'
            )
        self.methods = getattr(handler, '_bobo_methods', None)

        path_raw = getattr(
            handler, '_bobo_route', '/' + handler.__name__
            )
        self.path = (prefix.rstrip('/') + '/' +
                     path_raw.lstrip('/')).replace('//', '/')

        self.plan = FunctionCallPlan(
            handler, getattr(handler, '_bobo_params', 'params')
            )
        self.regex = self._compile(self.path)

    def _compile(self, path):
        """Converts /path/:id into a named regex group."""
        regex_string = re.sub(
            r'/:([a-zA-Z]\w*)', r'/(?P<\1>[^/]+)', path
            )
        return re.compile(regex_string + '$')

    def handle(self, request_object):
        """
        Executes the handler and transforms the return value into a Response.
        """
        if self.methods and request_object.method not in self.methods:
            return None

        matcher = self.regex.match(request_object.path_info)
        if matcher is None:
            return None

        kwargs = matcher.groupdict()
        source = getattr(request_object, self.plan.attr_source)

        # JSON Input Check
        json_data = None
        if request_object.content_type == 'application/json':
            try:
                json_data = request_object.json
            except ValueError:
                return webob.exc.HTTPBadRequest(
                    explanation="Invalid JSON body"
                    )

        for name in self.plan.names_required:
            if name in kwargs:
                continue

            value = source.get(name)
            if value is None and json_data:
                value = json_data.get(name)

            if value is None:
                return webob.exc.HTTPBadRequest(
                    explanation=f"Missing: {name}"
                    )
            kwargs[name] = value

        # Execute Handler
        rv = self.handler(request_object, **kwargs)

        # --- Automatic Response Generation Logic ---

        # 1. Direct Response
        if isinstance(rv, webob.Response):
            return rv

        # 2. JSON Marshalling
        if 'application/json' in self.content_type:
            try:
                body = json.dumps(rv).encode('utf-8')
                return webob.Response(
                    body=body, content_type=self.content_type
                    )
            except (TypeError, ValueError) as e:
                raise TypeError(f"Failed to marshal JSON: {e}")

        # 3. String Handling (Unicode/Bytes)
        if isinstance(rv, (str, bytes)):
            if isinstance(rv, bytes):
                return webob.Response(
                    body=rv, content_type=self.content_type
                    )

            # Determine encoding from content_type or default to UTF-8
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

        # 4. Fallback: Bobo raises TypeError for non-string/non-response non-JSON
        raise TypeError(
            f"Handler {self.handler.__name__} returned unsupported type: {type(rv).__name__}. "
            f"Expected Response, string, or JSON-serializable object."
            )


# ---------------------------------------------------------------------------
#  3. Decorators (Static Metadata Tagging)
# ---------------------------------------------------------------------------


def _tag(fn, route, methods, content_type, source):
    fn._bobo_route = route
    fn._bobo_methods = methods
    fn._bobo_content_type = content_type
    fn._bobo_params = source
    return fn


def query(route, content_type='text/html; charset=UTF-8'):
    """Decorator for GET/POST requests, defaults to HTML output."""
    def decorator(fn):
        return _tag(
            fn, route, ('GET', 'POST', 'HEAD'), content_type, 'params'
            )

    return decorator


def post(route, content_type='application/json'):
    """Decorator for POST requests, defaults to JSON output."""
    def decorator(fn):
        return _tag(fn, route, ('POST', ), content_type, 'POST')

    return decorator


# ---------------------------------------------------------------------------
#  4. The Application (Segmented Routing Forest)
# ---------------------------------------------------------------------------


class Application:
    """WSGI Application using bucket-based routing for performance."""
    def __init__(self, resources=None, prefix=""):
        self.prefix = prefix
        self.buckets = {}
        self.dynamics = []
        for rz in resources or ():
            self.register(rz)

    def register(self, fn):
        route = Route(fn, prefix=self.prefix)
        seg = route.path.lstrip('/').split('/')[0]
        if seg and not seg.startswith(':'):
            self.buckets.setdefault(seg, []).append(route)
        else:
            self.dynamics.append(route)

    def __call__(self, environ, start_response):
        request_object = webob.Request(environ)
        seg = request_object.path_info.lstrip('/').split('/', 1)[0]

        for route in [*self.buckets.get(seg, []), *self.dynamics]:
            if rsp := route.handle(request_object):
                return rsp(environ, start_response)

        return webob.exc.HTTPNotFound()(environ, start_response)

    @classmethod
    def from_module(cls, name, prefix=""):
        module = sys.modules[name]
        handlers = [
            value for value in vars(module).values()
            if hasattr(value, '_bobo_route')
            ]
        return cls(handlers, prefix=prefix)
