# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Front-door behaviour: static caching, SPA fallback, path guards, the config
overlay, simulator-environment routing, the restart guard, the /ws + /worldstate proxy, and /settings.

Pins the contract the aiohttp rewrite must keep. Part of the fast (no-ROS)
pytest bucket."""

import gzip
import json
import stat
import uuid
from urllib.parse import parse_qs, urlsplit

from conftest import fake_ws_upstream, make_app_root, serve, sync

CONTROL_NOW = 1_800_000_000.0


def make_environment_control(tmp_path):
    requests = tmp_path / "control/requests"
    status = tmp_path / "control/status"
    status.mkdir(parents=True)
    requests.mkdir(parents=True)
    catalog = {
        "schema_version": 1,
        "active": {"id": "apartment", "display_name": "Apartment", "fingerprint": "a" * 64},
        "environments": [
            {"id": "apartment", "display_name": "Apartment"},
            {"id": "gallery", "display_name": "Gallery"},
        ],
        "switch": None,
    }
    (status / "heartbeat.json").write_text(json.dumps({"schema_version": 1, "pid": 1234, "updated_at": CONTROL_NOW}))
    (status / "catalog.json").write_text(json.dumps(catalog))
    return requests, status, catalog


def control_overrides(requests, status):
    return {
        "WEBAPP_SIM_CONTROLS": True,
        "SIM_ENVIRONMENT_MUTATION_ENABLED": True,
        "SIM_ENVIRONMENT_ALLOWED_HOSTS": {"localhost", "127.0.0.1"},
        "SIM_ENVIRONMENT_REQUESTS_DIR": requests,
        "SIM_ENVIRONMENT_STATUS_DIR": status,
        "SIM_ENVIRONMENT_TIME": lambda: CONTROL_NOW,
    }


def make_sim_viewer(tmp_path):
    viewer = tmp_path / "sim/viewer"
    public = viewer / "public"
    for directory in (public / "environments/gallery/collisions", public / "environments/gallery/rooms"):
        directory.mkdir(parents=True)
    (public / "environments/gallery/gallery.glb").write_bytes(b"ACTIVE GALLERY")
    (public / "environments/gallery/collisions/hulls.f32").write_bytes(b"ACTIVE HULLS")
    (public / "environments/gallery/rooms/manifest.json").write_text('{"rooms": []}')
    (public / "environments/gallery/rooms/room.glb").write_bytes(b"ACTIVE ROOM")
    descriptor_path = tmp_path / "sim/assets/.active-environment.json"
    descriptor_path.parent.mkdir(parents=True)
    descriptor = {
        "schema_version": 1,
        "id": "gallery",
        "display_name": "Gallery",
        "fingerprint": "gallery-v1",
        "viewer": {
            "type": "glb",
            "model": "environments/gallery/gallery.glb",
            "collision_dir": "environments/gallery/collisions",
        },
    }
    return viewer, descriptor_path, descriptor


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
async def test_config_env_overlay(tmp_path):
    root = make_app_root(tmp_path)
    async with serve(ROOT=root, WEBAPP_SIM_CONTROLS=True, SIM_ENVIRONMENT_MUTATION_ENABLED=True) as (s, base):
        r = await s.get(base + "/config.json")
        assert r.headers["Cache-Control"] == "no-cache"
        cfg = await r.json()
        assert cfg["base"] is True and cfg["simControls"] is True
        assert cfg["simEnvironmentControls"] is True
        lan = await s.get(base + "/config.json", headers={"Host": "robot.local"})
        assert (await lan.json())["simEnvironmentControls"] is False
    async with serve(ROOT=root, WEBAPP_SIM_CONTROLS=True, SIM_ENVIRONMENT_MUTATION_ENABLED=False) as (s, base):
        cfg = await (await s.get(base + "/config.json")).json()
        assert cfg["simEnvironmentControls"] is False
        blocked = await s.post(
            base + "/sim-environment/switch",
            json={"id": "gallery"},
            headers={"X-Requested-By": "innate-webapp", "Origin": base},
        )
        assert blocked.status == 403
    async with serve(ROOT=root, WEBAPP_SIM_CONTROLS=False) as (s, base):
        cfg = await (await s.get(base + "/config.json")).json()
        assert "simControls" not in cfg


@sync
async def test_sim_environment_assets_and_local_control_round_trip(tmp_path):
    root = make_app_root(tmp_path)
    viewer, descriptor_path, descriptor = make_sim_viewer(tmp_path)
    descriptor_path.write_text(json.dumps(descriptor))
    overrides = {
        "ROOT": root,
        "SIM_VIEWER_ROOT": viewer,
        "ACTIVE_ENVIRONMENT_PATH": descriptor_path,
    }
    async with serve(**overrides) as (session, base):
        manifest = await session.get(base + "/sim-environment/manifest.json")
        assert manifest.headers["Cache-Control"] == "no-store, max-age=0"
        assert await manifest.json() == descriptor

        first = await session.get(base + "/sim-environment/scene.glb", allow_redirects=False)
        first_location = first.headers["Location"]
        assert parse_qs(urlsplit(first_location).query)["fingerprint"] == ["gallery-v1"]
        assert await (await session.get(base + first_location)).read() == b"ACTIVE GALLERY"

        descriptor["fingerprint"] = "gallery-v2"
        descriptor_path.write_text(json.dumps(descriptor))
        assert (await session.get(base + first_location, allow_redirects=False)).status == 412

        descriptor["fingerprint"] = "gallery-v3"
        descriptor["viewer"] = {
            "type": "split-glb",
            "manifest": "environments/gallery/rooms/manifest.json",
            "base_dir": "environments/gallery/rooms",
            "collision_dir": "environments/gallery/collisions",
        }
        descriptor_path.write_text(json.dumps(descriptor))
        for path in ("/sim-environment/layout.json", "/sim-environment/rooms/room.glb"):
            bound = await session.get(base + path, allow_redirects=False)
            assert bound.status == 307
            assert parse_qs(urlsplit(bound.headers["Location"]).query)["fingerprint"] == ["gallery-v3"]
            assert (await session.get(base + bound.headers["Location"])).status == 200

    requests, status, catalog = make_environment_control(tmp_path)
    async with serve(ROOT=root, **control_overrides(requests, status)) as (session, base):
        assert (await (await session.get(base + "/config.json")).json())["simEnvironmentControls"] is True
        response = await session.get(base + "/sim-environments.json")
        assert response.status == 200
        assert response.headers["Cache-Control"] == "no-store, max-age=0"
        assert await response.json() == catalog

        headers = {"X-Requested-By": "innate-webapp", "Origin": base}
        switch_body = {"id": "gallery"}
        for forbidden_headers in (
            {"X-Requested-By": "innate-webapp", "Host": "evil.example", "Origin": "http://evil.example"},
            {"X-Requested-By": "innate-webapp", "Origin": "http://evil.example"},
        ):
            forbidden = await session.post(
                base + "/sim-environment/switch", json=switch_body, headers=forbidden_headers
            )
            assert forbidden.status == 403
        invalid = await session.post(base + "/sim-environment/switch", json={"id": "../bad"}, headers=headers)
        assert invalid.status == 400

        # The cap applies after request decompression, not to Content-Length.
        compressed = gzip.compress(json.dumps({"padding": "x" * 5000}).encode())
        gzipped = await session.post(
            base + "/sim-environment/switch",
            data=compressed,
            headers={**headers, "Content-Type": "application/json", "Content-Encoding": "gzip"},
        )
        assert gzipped.status == 413

        heartbeat_path = status / "heartbeat.json"
        heartbeat_path.write_text(json.dumps({"schema_version": 1, "updated_at": float("nan")}))
        unavailable = await session.post(base + "/sim-environment/switch", json=switch_body, headers=headers)
        assert unavailable.status == 503
        heartbeat_path.write_text(json.dumps({"schema_version": 1, "updated_at": CONTROL_NOW}))

        (status / "stopping").touch()
        stopping = await session.post(base + "/sim-environment/switch", json=switch_body, headers=headers)
        assert stopping.status == 503
        (status / "stopping").unlink()

        queued = await session.post(base + "/sim-environment/switch", json=switch_body, headers=headers)
        assert queued.status == 202
        current = requests / "current.json"
        assert stat.S_IMODE(current.stat().st_mode) == 0o644
        accepted = await queued.json()
        request_id = accepted["request_id"]
        assert str(uuid.UUID(request_id)) == request_id
        assert json.loads(current.read_text()) == {"request_id": request_id, "id": "gallery"}
        assert accepted["target"] == {"id": "gallery", "display_name": "Gallery"}

        # The catalog can lag the accepted mailbox by one controller poll. GET
        # must expose the server-generated UUID immediately.
        overlaid = await session.get(base + "/sim-environments.json")
        assert overlaid.status == 200
        assert (await overlaid.json())["switch"] == {
            "request_id": request_id,
            "target": {"id": "gallery", "display_name": "Gallery"},
            "state": "queued",
            "message": "Waiting for the simulator environment controller...",
        }
        conflict = await session.post(base + "/sim-environment/switch", json={"id": "apartment"}, headers=headers)
        assert conflict.status == 409

        (status / "stopping").touch()
        stopping_catalog = await session.get(base + "/sim-environments.json")
        assert stopping_catalog.status == 503
        assert (await stopping_catalog.json())["switch"]["state"] == "queued"
        (status / "stopping").unlink()

        catalog["switch"] = {
            "request_id": request_id,
            "target": {"id": "gallery", "display_name": "Gallery"},
            "state": "running",
            "message": "Starting navigation…",
        }
        (status / "catalog.json").write_text(json.dumps(catalog))
        (status / "heartbeat.json").write_text(json.dumps({"schema_version": 1, "updated_at": CONTROL_NOW - 10}))
        abandoned = await session.get(base + "/sim-environments.json")
        assert abandoned.status == 503
        assert (await abandoned.json())["switch"]["state"] == "running"

        # A stale running controller is fail-closed. Once it publishes a
        # terminal catalog, that result remains readable without its heartbeat.
        current.unlink()
        catalog["switch"].update({"state": "ready", "message": "Gallery is ready."})
        (status / "catalog.json").write_text(json.dumps(catalog))
        assert (await session.get(base + "/sim-environments.json")).status == 200


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
