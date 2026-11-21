"""CORS 预检：Tauri v2 打包环境 Origin（http://tauri.localhost）必须放行。"""


def test_options_preflight_tauri_localhost(client):
    r = client.options(
        "/api/v1/projects",
        headers={
            "Origin": "http://tauri.localhost",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-auth-token,content-type",
        },
    )
    assert r.status_code == 200, r.text
    assert r.headers.get("access-control-allow-origin") == "http://tauri.localhost"
    assert "x-auth-token" in (r.headers.get("access-control-allow-headers") or "")


def test_options_preflight_localhost_dev(client):
    r = client.options(
        "/api/v1/projects",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert r.status_code == 200


def test_cors_rejects_foreign_origin(client):
    r = client.options(
        "/api/v1/projects",
        headers={"Origin": "https://evil.example.com", "Access-Control-Request-Method": "GET"},
    )
    assert r.status_code == 400
