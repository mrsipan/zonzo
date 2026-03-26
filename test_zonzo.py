import pytest
from webtest import TestApp

from zonzo import Application, post, query

# --- Mock Handlers for Testing ---


@query("/hello/:name")
def greet(request, name):
    return f"Hello, {name}!"


@post("/api/update")
def update_user(request, user_id, email):
    # Testing JSON injection and JSON response generation
    return {"status": "success", "updated_id": user_id, "email": email}


@query("/defaults")
def with_defaults(request, mode="guest"):
    return f"Mode: {mode}"


@query("/json_echo", content_type="application/json")
def echo_json(request, data):
    # Testing manual return of a dict for auto-marshalling
    return data


# --- Test Cases ---


@pytest.fixture
def app():
    """Provides a WebTest-wrapped instance of our Application."""
    handlers = [greet, update_user, with_defaults, echo_json]
    wsgi_app = Application(handlers)
    return TestApp(wsgi_app)


def test_basic_routing(app):
    """Verify that path variables (:name) are parsed and injected."""
    res = app.get("/hello/Gemini")
    assert res.status_code == 200
    assert res.text == "Hello, Gemini!"
    assert res.content_type == "text/html"


def test_json_body_injection(app):
    """Verify that application/json body keys are mapped to function arguments."""
    payload = {"user_id": "123", "email": "ana@example.com"}
    res = app.post_json("/api/update", payload)

    assert res.status_code == 200
    assert res.json["status"] == "success"
    assert res.json["updated_id"] == "123"
    assert res.json["email"] == "ana@example.com"
    assert res.content_type == "application/json"


def test_missing_arguments_returns_400(app):
    """If a required argument is missing from both path and body, return 400."""
    # 'email' is missing here
    res = app.post_json(
        "/api/update", {"user_id": "123"}, expect_errors=True
        )
    assert res.status_code == 400
    assert "Missing argument: email" in res.text


def test_query_params_injection(app):
    """Verify that standard query parameters are injected."""
    res = app.get("/defaults?mode=admin")
    assert res.text == "Mode: guest"


def test_default_values(app):
    """Verify that if a parameter is missing, the function's default is used."""
    res = app.get("/defaults")
    assert res.text == "Mode: guest"


def test_invalid_json_body(app):
    """Ensure malformed JSON results in a 400 Bad Request."""
    headers = {"Content-Type": "application/json"}
    res = app.post(
        "/api/update",
        '{"invalid": json',
        headers=headers,
        expect_errors=True
        )
    assert res.status_code == 400
    assert "Malformed JSON body" in res.text


def test_404_not_found(app):
    """Ensure non-existent routes return a 404."""
    res = app.get("/does-not-exist", expect_errors=True)
    assert res.status_code == 404
