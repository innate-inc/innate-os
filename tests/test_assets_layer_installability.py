"""Publication must reject layers that the launcher's extractor cannot install."""

import importlib.util
import io
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sim/launcher"))
import oci  # noqa: E402

spec = importlib.util.spec_from_file_location("verify_assets_image", ROOT / "ci/verify_assets_image.py")
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


def layer(name, kind=tarfile.REGTYPE):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        member = tarfile.TarInfo(name)
        member.type = kind
        if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
            member.linkname = "/build/assets/texture.png"
        tar.addfile(member)
    return buf.getvalue()


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_publication_and_install_reject_texture_links(tmp_path, monkeypatch, kind):
    blob = layer("work/apartment_split_v2/.cabinet/cache/Sala_Cozinha.png", kind)
    path = tmp_path / "layer.tar"
    path.write_bytes(blob)
    monkeypatch.setattr(oci, "fetch_layer", lambda repo, digest, buf, token, **kw: buf.write(blob))
    with pytest.raises(oci.OciError, match="unsupported member type"):
        verify.subtree_of("repo", "digest", "token")
    with pytest.raises(oci.OciError, match="unsupported member type"):
        oci.safe_extract(path, tmp_path / "out")


def test_publication_rejects_build_machine_cache(monkeypatch):
    blob = layer("work/apartment_split_v2/.cabinet/cache/manifest.json")
    monkeypatch.setattr(oci, "fetch_layer", lambda repo, digest, buf, token, **kw: buf.write(blob))
    with pytest.raises(oci.OciError, match="host-local cabinet cache"):
        verify.subtree_of("repo", "digest", "token")


def test_regular_texture_publishes_and_installs(tmp_path, monkeypatch):
    blob = layer("work/apartment_visual/Sala_Cozinha/Sala_Cozinha.png")
    path = tmp_path / "layer.tar"
    path.write_bytes(blob)
    monkeypatch.setattr(oci, "fetch_layer", lambda repo, digest, buf, token, **kw: buf.write(blob))
    assert verify.subtree_of("repo", "digest", "token") == "work"
    oci.safe_extract(path, tmp_path / "out")
    assert (tmp_path / "out/work/apartment_visual/Sala_Cozinha/Sala_Cozinha.png").is_file()
