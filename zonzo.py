##############################################################################
#
# Optimized Web Framework (High-Performance "Bobo" Evolution)
# Features: Segmented Routing, Precomputed Call Plans, Safe Path Resolution
#
##############################################################################

import inspect
import logging
import re
from functools import lru_cache

import webob
import webob.exc

# Initialize logging for the framework
framework_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  Performance Core: The Call Plan
# ---------------------------------------------------------------------------


class FunctionCallPlan:
    """
    Analyzes a function signature once at startup to create a 'cheat sheet'
    for argument injection, bypassing the slow 'inspect' module at runtime.
    """
    __slots__ = (
        'required_argument_names', 'default_values',
        'parameter_source_attribute'
        )

    def __init__(
        self, handler_function, parameter_source_attribute='params'
        ):
        function_signature = inspect.signature(handler_function)
        parameters = list(function_signature.parameters.values())

        # We assume the first parameter is always the 'request' object.
        # We map the rest.
        self.required_argument_names = [
            param.name for param in parameters[1:]
            if param.default is inspect.Parameter.empty
            ]
        self.default_values = {
            param.name: param.default
            for param in parameters[1:]
            if param.default is not inspect.Parameter.empty
            }
        self.parameter_source_attribute = parameter_source_attribute


# ---------------------------------------------------------------------------
#  The Optimized Route Object
# ---------------------------------------------------------------------------


class OptimizedRoute:
    """
    A single route handler that encapsulates its own regex and injection plan.
    """
    __slots__ = (
        'handler_callable', 'execution_plan', 'compiled_regex',
        'allowed_methods', 'expected_content_type', 'route_path'
        )

    def __init__(self, handler_callable):
        self.handler_callable = handler_callable
        self.route_path = getattr(
            handler_callable, '_bobo_route',
            '/' + handler_callable.__name__
            )
        self.expected_content_type = getattr(
            handler_callable, '_bobo_content_type',
            'text/html; charset=UTF-8'
            )
        self.allowed_methods = getattr(
            handler_callable, '_bobo_methods', None
            )

        # Determine if we should look at 'params' (GET/POST) or specifically 'POST'
        source_attr = getattr(
            handler_callable, '_bobo_params', 'params'
            )
        self.execution_plan = FunctionCallPlan(
            handler_callable, source_attr
            )

        # Pre-compile the regex pattern
        self.compiled_regex = self._compile_route_to_regex(
            self.route_path
            )

    def _compile_route_to_regex(self, path_pattern):
        """Converts Bobo-style /:var patterns into compiled regular expressions."""
        if not path_pattern.startswith('/'):
            path_pattern = '/' + path_pattern

        # Replace /:name with named regex groups
        regex_string = re.sub(
            r'/:([a-zA-Z]\w*)', r'/(?P<\1>[^/]+)', path_pattern
            )
        return re.compile(regex_string + '$')

    def handle_request(self, request_object, current_path, http_method):
        """
        Attempts to match the path and method, then executes the handler.
        Returns a WebOb Response if matched, otherwise None.
        """
        if self.allowed_methods and http_method not in self.allowed_methods:
            return None

        path_match = self.compiled_regex.match(current_path)
        if not path_match:
            return None

        # 1. Start with values extracted directly from the URL path
        handler_kwargs = path_match.groupdict()

        # 2. Extract remaining required arguments from the primary data source
        primary_data_source = getattr(
            request_object,
            self.execution_plan.parameter_source_attribute
            )

        # Optimization: Local reference to the cached JSON if content-type is JSON
        json_payload = None
        if request_object.content_type == 'application/json':
            try:
                json_payload = request_object.json
            except ValueError:
                return webob.exc.HTTPBadRequest(
                    explanation="Invalid JSON payload"
                    )

        for argument_name in self.execution_plan.required_argument_names:
            if argument_name in handler_kwargs:
                continue

            # Check primary source (form/query), fallback to JSON
            value = primary_data_source.get(argument_name)
            if value is None and json_payload is not None:
                value = json_payload.get(argument_name)

            if value is None:
                return webob.exc.HTTPBadRequest(
                    explanation=
                    f"Missing required parameter: {argument_name}"
                    )

            handler_kwargs[argument_name] = value

        # 3. Call the actual function
        rv = self.handler_callable(request_object, **handler_kwargs)

        # 4. Wrap the result in a proper response object
        if isinstance(rv, webob.Response):
            return rv

        return webob.Response(
            body=str(rv).encode('utf-8'),
            content_type=self.expected_content_type
            )


# ---------------------------------------------------------------------------
#  The Application Manager
# ---------------------------------------------------------------------------


class Application:
    """
    Main WSGI Entry point. Uses segmented routing to ensure O(1) or O(small N)
    dispatch times even as the route count grows.
    """
    def __init__(self, resource_list=None):
        # segmented_routes: { 'first_path_segment': [list_of_routes] }
        self.segmented_routes = {}
        self.dynamic_catchall_routes = []

        if resource_list:
            for resource in resource_list:
                self.register_resource(resource)

    def register_resource(self, resource_callable):
        """Builds a route object and places it in the correct search bucket."""
        route_instance = OptimizedRoute(resource_callable)

        # Peek at the first segment of the path to categorize it
        path_segments = route_instance.route_path.lstrip('/').split('/')
        first_segment = path_segments[0]

        if first_segment and not first_segment.startswith(':'):
            if first_segment not in self.segmented_routes:
                self.segmented_routes[first_segment] = []
            self.segmented_routes[first_segment].append(route_instance)
        else:
            # Routes starting with /:id or the root / go here
            self.dynamic_catchall_routes.append(route_instance)

    def __call__(self, wsgi_environment, start_response_callback):
        request = webob.Request(wsgi_environment)
        current_path = request.path_info
        http_method = request.method

        # Performance optimization: Segmented lookup
        # If path is /users/profile, we only search the 'users' bucket.
        url_segments = current_path.lstrip('/').split('/', 1)
        search_bucket = self.segmented_routes.get(url_segments[0], [])

        # Search the categorized bucket, then the dynamic catch-alls
        for route in (search_bucket + self.dynamic_catchall_routes):
            response = route.handle_request(
                request, current_path, http_method
                )
            if response is not None:
                return response(
                    wsgi_environment, start_response_callback
                    )

        # If no route found
        error_404 = webob.exc.HTTPNotFound()
        return error_404(wsgi_environment, start_response_callback)


# ---------------------------------------------------------------------------
#  Utilities: Safe Resource Resolution
# ---------------------------------------------------------------------------


def resolve_python_path_safely(path_string):
    """
    Resolves a 'module:attribute' string by walking the attribute tree.
    Faster and safer than eval().
    """
    if ':' not in path_string:
        raise ValueError(
            "Path must be in 'module:object' or 'module:class.method' format."
            )

    module_name, attribute_chain = path_string.split(':', 1)
    target_object = __import__(module_name, fromlist=['*'])

    for attribute_name in attribute_chain.split('.'):
        target_object = getattr(target_object, attribute_name)

    return target_object
