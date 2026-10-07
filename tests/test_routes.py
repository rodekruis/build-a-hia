def test_index_returns_home_page(client):
    response = client.get("/")

    assert response.status_code == 200
    assert b"Build-a-HIA" in response.data


def test_health_returns_ok_without_request_log(client, caplog):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json == {"status": "ok"}
    assert "HTTP request" not in caplog.text


def test_unknown_route_returns_custom_not_found_page(client):
    response = client.get("/missing")

    assert response.status_code == 404
    assert b"Page not found" in response.data


def test_responses_include_security_headers(client):
    response = client.get("/")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "form-action 'self'" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"


def test_hsts_is_sent_when_secure_cookies_are_configured(app, client):
    assert "Strict-Transport-Security" not in client.get("/").headers

    app.config["SESSION_COOKIE_SECURE"] = True
    assert "max-age=31536000" in client.get("/").headers["Strict-Transport-Security"]


def test_static_files_stay_cacheable(client):
    response = client.get("/static/css/site.css")

    assert "no-store" not in response.headers.get("Cache-Control", "")
    response.close()
