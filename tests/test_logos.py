import hashlib

from app import main

PNG = b"\x89PNG\r\n\x1a\n" + b"fake image bytes"


def _upload(client, body=PNG, name="logo.png", ctype="image/png"):
    return client.post("/api/logos", files={"file": (name, body, ctype)})


def test_upload_and_fetch(client):
    r = _upload(client)
    assert r.status_code == 200
    entry = r.json()
    logo_id = hashlib.sha256(PNG).hexdigest()[:16]
    assert entry["id"] == logo_id
    assert entry["url"] == f"/api/logos/{logo_id}"
    assert entry["contentType"] == "image/png"
    assert entry["sizeBytes"] == len(PNG)
    assert entry["fileName"] == "logo.png"

    got = client.get(f"/api/logos/{logo_id}")
    assert got.status_code == 200
    assert got.content == PNG
    assert got.headers["content-type"] == "image/png"

    assert client.get("/api/logos").json() == [entry]


def test_reupload_dedups(client):
    first = _upload(client, name="a.png").json()
    second = _upload(client, name="b.png").json()
    assert second == first  # same content, same id, original metadata kept
    assert len(client.get("/api/logos").json()) == 1


def test_empty_upload(client):
    assert _upload(client, body=b"").status_code == 400


def test_upload_size_limit(client):
    assert _upload(client, body=b"x" * main.LOGO_MAX_BYTES).status_code == 200
    assert _upload(client, body=b"y" * (main.LOGO_MAX_BYTES + 1)).status_code == 413
    assert len(client.get("/api/logos").json()) == 1


def test_fetch_errors(client):
    assert client.get("/api/logos/NOT-A-VALID-ID").status_code == 400
    assert client.get("/api/logos/0123456789abcdeF").status_code == 400
    assert client.get("/api/logos/0123456789abcdef").status_code == 404


def test_logo_assignments(client):
    assert client.get("/api/logo-assignments/dev1").status_code == 404
    a = {"kind": "generic", "ref": "rocket", "initialsOverlay": True}
    assert client.put("/api/logo-assignments/dev1", json=a).json() == a
    assert client.get("/api/logo-assignments/dev1").json() == a

    # PUT replaces the whole assignment.
    b = {"kind": "upload", "ref": "0123456789abcdef"}
    assert client.put("/api/logo-assignments/dev1", json=b).json() == {**b, "initialsOverlay": None}
    assert client.get("/api/logo-assignments/dev1").json() == {**b, "initialsOverlay": None}


def test_logo_assignment_validation(client):
    assert client.put("/api/logo-assignments/dev1", json={"kind": "emoji", "ref": "x"}).status_code == 422
    assert client.put("/api/logo-assignments/dev1", json={"kind": "generic"}).status_code == 422
    assert client.put("/api/logo-assignments/dev1", json={"kind": "generic", "ref": "x", "extra": 1}).status_code == 422
    assert client.get("/api/logo-assignments/bad.key").status_code == 400
    assert client.put("/api/logo-assignments/bad$key", json={"kind": "generic", "ref": "x"}).status_code == 400


def test_generic_icons_list(client):
    names = client.get("/api/generic-icons").json()
    assert names == sorted(names)
    assert "rocket" in names and "terminal" in names


def test_generic_icon_fetch(client):
    r = client.get("/api/generic-icons/rocket")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/svg+xml"
    assert b"<svg" in r.content


def test_generic_icon_errors(client):
    assert client.get("/api/generic-icons/does-not-exist").status_code == 404
    assert client.get("/api/generic-icons/Rocket").status_code == 400
    assert client.get("/api/generic-icons/bad_name").status_code == 400
    assert client.get("/api/generic-icons/..%2Fmain").status_code in (400, 404)


def test_generic_icons_missing_dir(client, monkeypatch, tmp_path):
    monkeypatch.setattr(main, "GENERIC_ICONS_DIR", tmp_path / "none")
    assert client.get("/api/generic-icons").json() == []
