import inspect
import logging
import re
import sys

import webob
import webob.exc

# ---------------------------------------------------------------------------
#  1. The Call Plan (Zero-Reflection Runtime)
# ---------------------------------------------------------------------------


class FunctionCallPlan:
    """Pre-analyzes function signatures to avoid 'inspect' during request_objects."""
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


class OptimizedRoute:
    __slots__ = (
        'handler', 'plan', 'regex', 'methods', 'content_type', 'path'
        )

    def __init__(self, handler, prefix=""):
        self.handler = handler
        self.content_type = getattr(
            handler, '_bobo_content_type', 'text/html; charset=UTF-8'
            )
        self.methods = getattr(handler, '_bobo_methods', None)

        # Build Path with Prefix
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
        # Converts /users/:id into a named regex group
        regex_string = re.sub(
            r'/:([a-zA-Z]\w*)', r'/(?P<\1>[^/]+)', path
            )
        return re.compile(regex_string + '$')

    def handle(self, request_object):
        if self.methods and request_object.method not in self.methods:
            return None

        matcher = self.regex.match(request_object.path_info)

        if matcher is None:
            return None

        kwargs = matcher.groupdict()
        source = getattr(request_object, self.plan.attr_source)

        # JSON Cache Check
        json_data = None
        if request_object.content_type == 'application/json':
            json_data = request_object.json

        for name in self.plan.names_required:
            if name in kwargs:
                continue
            value = source.get(name) or (
                json_data.get(name) if json_data else None
                )

            return (
                webob.exc.HTTPBadRequest(
                    explanation=f"Missing: {name}"
                    )
                ) if value is None else kwargs.update({name: value})

        if isinstance(
            rv := self.handler(request_object, **kwargs),
            webob.Response,
            ):
            return rv
        return webob.Response(
            body=str(rv).encode('utf-8'),
            content_type=self.content_type
            )


# ---------------------------------------------------------------------------
#  3. Decorators (Static Metadata Tagging)
# ---------------------------------------------------------------------------


def _tag(
    fn,
    route,
    methods,
    content_type='text/html; charset=UTF-8',
    source='params'
    ):

    fn._bobo_route = route
    fn._bobo_methods = methods
    fn._bobo_content_type = content_type
    fn._bobo_params = source
    return fn


def query(route, content_type='text/html; charset=UTF-8'):
    return lambda f: _tag(
        f, route, ('GET', 'POST', 'HEAD'), content_type, 'params'
        )


def post(route, content_type='application/json'):
    return lambda f: _tag(f, route, ('POST', ), content_type, 'POST')


# ---------------------------------------------------------------------------
#  4. The Application (Segmented Routing Forest)
# ---------------------------------------------------------------------------


class Application:
    def __init__(self, resources=None, prefix=""):
        self.prefix = prefix
        self.buckets = {}
        self.dynamics = []
        for rz in resources or ():
            self.register(rz)

    def register(self, fn):
        route = OptimizedRoute(fn, prefix=self.prefix)
        # O(1) Segment Partitioning
        seg = route.path.lstrip('/').split('/')[0]
        if seg and not seg.startswith(':'):
            self.buckets.setdefault(seg, []).append(route)
        else:
            self.dynamics.append(route)

    def __call__(self, environ, start_response):
        request_object = webob.Request(environ)
        seg = request_object.path_info.lstrip('/').split('/', 1)[0]

        for route in (self.buckets.get(seg, []) + self.dynamics):
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
