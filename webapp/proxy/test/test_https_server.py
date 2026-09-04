# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Front-door behaviour: static caching, SPA fallback, path guards, sim assets,
the config overlay, the restart guard, the /ws + /worldstate proxy, and /settings.

Pins the contract the aiohttp rewrite must keep. Part of the fast (no-ROS)
pytest bucket."""

from conftest import H, fake_ws_upstream, make_app_root, serve, sync


@sync
async def test_static_etag_304_and_range(tmp_path):
    root = make_app_root(tmp_path)
    async with serve(ROOT=root) as (s, base):
        r = await s.get(base + "/assets/app.js")
        assert r.status == 200
        assert r.headers["Cache-Control"] == "no-cache"
        assert r.headers["Accept-Ranges"] == "bytes"
        etag = r.headers["ETag"]
        await r.read()

        # a matching If-None-Match -> bodyless 304
        r2 = await s.get(base + "/assets/app.js", headers={"If-None-Match": etag})
        assert r2.status == 304
        assert await r2.read() == b""

        # Range -> 206 with a Content-Range
        r3 = await s.get(base + "/assets/app.js", headers={"Range": "bytes=0-3"})
        assert r3.status == 206
        assert r3.headers["Content-Range"].startswith("bytes 0-3/")
        assert len(await r3.read()) == 4


@sync
async def test_gzip_text_assets(tmp_path):
    root = make_app_root(tmp_path)
    big = "// filler\n" * 4000
    (root / "assets" / "big.js").write_text(big)
    async with serve(ROOT=root) as (s, base):
        r = await s.get(base + "/assets/big.js", headers={"Accept-Encoding": "gzip"})
        assert r.status == 200
        assert r.headers["Content-Encoding"] == "gzip"
        assert r.headers["Vary"] == "Accept-Encoding"
        assert int(r.headers["Content-Length"]) < len(big) // 4
        assert await r.text() == big  # aiohttp inflates it; the bytes round-trip
        gz_etag = r.headers["ETag"]

        # the compressed representation revalidates to a bodyless 304 of its own
        r2 = await s.get(base + "/assets/big.js", headers={"Accept-Encoding": "gzip", "If-None-Match": gz_etag})
        assert r2.status == 304 and await r2.read() == b""

        # a client that takes no gzip gets the plain file, under a different ETag,
        # and the identity representation declares the same Vary as the gzip one
        r3 = await s.get(base + "/assets/big.js", headers={"Accept-Encoding": "identity"})
        assert r3.headers.get("Content-Encoding") is None
        assert r3.headers["Vary"] == "Accept-Encoding"
        assert r3.headers["ETag"] != gz_etag
        assert await r3.text() == big

        # an explicit rejection (q=0) means identity too — "gzip" as a substring
        # is not acceptance; a bare wildcard is
        r4 = await s.get(base + "/assets/big.js", headers={"Accept-Encoding": "gzip;q=0, identity"})
        assert r4.headers.get("Content-Encoding") is None
        await r4.read()
        r5 = await s.get(base + "/assets/big.js", headers={"Accept-Encoding": "*"})
        assert r5.headers["Content-Encoding"] == "gzip"
        assert await r5.text() == big


@sync
async def test_gzip_skipped_where_it_does_not_pay(tmp_path):
    root = make_app_root(tmp_path)
    (root / "assets" / "clip.mp4").write_bytes(b"\x00" * 8000)
    async with serve(ROOT=root) as (s, base):
        # app.js is well under GZIP_MIN_BYTES, and video is not a text type
        for path in ("/assets/app.js", "/assets/clip.mp4"):
            r = await s.get(base + path, headers={"Accept-Encoding": "gzip"})
            assert r.headers.get("Content-Encoding") is None, path
            assert r.headers["Accept-Ranges"] == "bytes", path
            await r.read()

        # a Range request stays on the file itself even for a gzippable type
        (root / "assets" / "big.css").write_text("body { color: red }\n" * 500)
        r = await s.get(base + "/assets/big.css", headers={"Accept-Encoding": "gzip", "Range": "bytes=0-3"})
        assert r.status == 206 and r.headers.get("Content-Encoding") is None
        assert len(await r.read()) == 4


@sync
async def test_vendor_assets_cache_forever(tmp_path):
    root = make_app_root(tmp_path)
    vendor = root / "public" / "vendor"
    vendor.mkdir(parents=True)
    (vendor / "three.module.min.r160.js").write_text("export const x = 1;\n" * 200)
    (vendor / "unversioned.js").write_text("export const y = 2;\n" * 200)
    async with serve(ROOT=root) as (s, base):
        r = await s.get(base + "/public/vendor/three.module.min.r160.js", headers={"Accept-Encoding": "gzip"})
        assert r.headers["Cache-Control"] == "public, max-age=31536000, immutable"
        assert r.headers["Content-Encoding"] == "gzip"
        await r.read()
        # immutable only where the filename names its version — an unversioned
        # vendor file replaced in place must not stay pinned for a year
        r2 = await s.get(base + "/public/vendor/unversioned.js")
        assert r2.headers["Cache-Control"] == "no-cache"
        await r2.read()
        # everything outside public/vendor stays no-cache
        r3 = await s.get(base + "/assets/app.js")
        assert r3.headers["Cache-Control"] == "no-cache"
        await r3.read()


@sync
async def test_spa_fallback_and_missing_asset(tmp_path):
    root = make_app_root(tmp_path)
    async with serve(ROOT=root) as (s, base):
        # extensionless routes (a deep link / refresh) get the app shell
        for path in ("/", "/settings-page", "/nav/maps/deep/link"):
            r = await s.get(base + path)
            assert r.status == 200
            assert "SHELL" in await r.text()
        # a genuinely missing *asset* (has a suffix) still 404s
        assert (await s.get(base + "/assets/missing.js")).status == 404


@sync
async def test_path_guards(tmp_path):
    root = make_app_root(tmp_path)
    (root / "secret.pem").write_text("PRIVATE KEY")
    (tmp_path / "outside.txt").write_text("SECRETDATA")  # a real file just outside ROOT
    async with serve(ROOT=root) as (s, base):
        # Security invariant: a traversal attempt never serves content from outside
        # ROOT. (The client/yarl normalises ../ and %2e%2e before it's sent, and the
        # is_relative_to(ROOT) guard backstops anything that slips through — either
        # way the outside file is never in the body.)
        for path in ("/%2e%2e/outside.txt", "/%2e%2e/%2e%2e/%2e%2e/%2e%2e/etc/passwd"):
            body = await (await s.get(base + path)).text()
            assert "SECRETDATA" not in body and "root:" not in body
        # an in-root .pem is refused outright
        assert (await s.get(base + "/secret.pem")).status == 404
        # a NUL byte in the path -> 404, not a 500 traceback
        assert (await s.get(base + "/%00")).status == 404


@sync
async def test_local_environment_assets_are_served(tmp_path):
    root = make_app_root(tmp_path)
    local_environments = tmp_path / "viewer-public" / "local-environments"
    scene = local_environments / "low-poly-town" / "scene.glb"
    scene.parent.mkdir(parents=True)
    scene.write_bytes(b"town glb")
    routes = dict(H.SIM_VIEWER_ROUTES)
    assert routes["/local-environments/"] == H.SIM_VIEWER_ROOT / "public" / "local-environments"
    routes["/local-environments/"] = local_environments

    async with serve(ROOT=root, SIM_VIEWER_ROUTES=routes) as (s, base):
        response = await s.get(base + "/local-environments/low-poly-town/scene.glb")
        assert response.status == 200
        assert response.headers["Content-Type"] == "model/gltf-binary"
        assert await response.read() == b"town glb"


@sync
async def test_config_env_overlay(tmp_path):
    root = make_app_root(tmp_path)
    async with serve(ROOT=root, WEBAPP_SIM_CONTROLS=True) as (s, base):
        r = await s.get(base + "/config.json")
        assert r.headers["Cache-Control"] == "no-cache"
        cfg = await r.json()
        assert cfg["base"] is True and cfg["simControls"] is True
    async with serve(ROOT=root, WEBAPP_SIM_CONTROLS=False) as (s, base):
        cfg = await (await s.get(base + "/config.json")).json()
        assert "simControls" not in cfg


@sync
async def test_restart_requires_header(tmp_path):
    # No X-Requested-By -> 403, and nothing is spawned. (The happy path would run
    # `innate restart`, so it's deliberately not exercised.)
    root = make_app_root(tmp_path)
    async with serve(ROOT=root) as (s, base):
        assert (await s.get(base + "/restart")).status == 403


@sync
async def test_ws_proxy_relays(tmp_path):
    root = make_app_root(tmp_path)
    async with fake_ws_upstream("ros:") as ros, fake_ws_upstream("world:") as world:
        async with serve(ROOT=root, ROSBRIDGE_URL=ros, WORLD_STATE_URL=world) as (s, base):
            async with s.ws_connect(base + "/ws") as ws:
                await ws.send_str("ping")
                assert (await ws.receive()).data == "ros:ping"
                await ws.send_bytes(b"\x01\x02")
                assert (await ws.receive()).data == b"\x01\x02"
            async with s.ws_connect(base + "/worldstate") as ws:
                await ws.send_str("tick")
                assert (await ws.receive()).data == "world:tick"


@sync
async def test_settings_url_serves_app_not_json(tmp_path):
    # /settings is the SPA route for the settings page; a browser navigation must
    # get the app shell, not the API JSON (which lives at /settings.json now).
    root = make_app_root(tmp_path)
    async with serve(ROOT=root) as (s, base):
        r = await s.get(base + "/settings")
        assert r.status == 200
        assert "SHELL" in await r.text()


@sync
async def test_settings_json_get(tmp_path, monkeypatch):
    import settings_store

    monkeypatch.setattr(settings_store, "read_overrides", lambda: {"foo": 1})
    monkeypatch.setattr(settings_store, "settings_path", lambda: tmp_path / "nope.yaml")
    root = make_app_root(tmp_path)
    async with serve(ROOT=root) as (s, base):
        body = await (await s.get(base + "/settings.json")).json()
        assert body == {"overrides": {"foo": 1}, "exists": False}


@sync
async def test_settings_json_post(tmp_path, monkeypatch):
    import settings_store

    seen = []

    def fake_apply(sets, clears):
        seen.append((sets, clears))
        return True, "applied"

    monkeypatch.setattr(settings_store, "apply_changes", fake_apply)
    root = make_app_root(tmp_path)
    async with serve(ROOT=root) as (s, base):
        r = await s.post(base + "/settings.json", json={"sets": [{"path": "a", "value": 1}], "clears": ["b"]})
        assert (await r.json()) == {"ok": True, "message": "applied"}
        # a malformed body (right Content-Type, bad JSON) -> 400, not a crash
        r2 = await s.post(base + "/settings.json", data="not json", headers={"Content-Type": "application/json"})
        assert r2.status == 400 and (await r2.json())["ok"] is False
        # a wrong Content-Type -> 400 too (request.json raises ContentTypeError, not ValueError)
        r3 = await s.post(base + "/settings.json", data="whatever", headers={"Content-Type": "text/plain"})
        assert r3.status == 400 and (await r3.json())["ok"] is False
    assert seen == [([{"path": "a", "value": 1}], ["b"])]
