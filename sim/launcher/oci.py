"""Pull a single OCI layer over plain HTTPS -- no Docker, no daemon, no deps.

`innate-sim assets` must work without Docker: VirtualMars notebooks want
sim/assets and nothing else. The whole protocol for a public GHCR package is
three GETs:

    GET /token?scope=repository:<repo>:pull&service=ghcr.io   -> {"token": ...}
    GET /v2/<repo>/manifests/<ref>                            -> index or manifest
    GET /v2/<repo>/blobs/<digest>                             -> a gzipped tar

A layer blob IS a gzipped tar, so "pull one layer" is that third GET piped into
tarfile. Since sim/Dockerfile.assets gives each subtree its own layer and never
rewrites an earlier one, one blob extracts to a complete subtree -- ~85 MB of
MuJoCo geometry instead of the whole ~156 MB image. The layer digest is the
integrity check, so nothing here needs a side-channel checksum.
"""

from __future__ import annotations

import hashlib
import json
import platform
import sys
import tarfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config import StackError, log
from dashboard import DIM, NC, active_step, format_bytes, render_progress_bar

REGISTRY = "ghcr.io"
_TIMEOUT_S = 600

# Both the OCI names and Docker's older media types: GHCR still answers with
# application/vnd.docker.distribution.manifest.list.v2+json for images pushed by
# buildx, so asking for only the OCI names gets a 404 on real packages.
_INDEX_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
)
_MANIFEST_TYPES = (
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)


class OciError(StackError):
    """Registry conversation failed; a StackError, like any other network step.

    `status` is the HTTP status when the registry answered, and None when it did
    not (DNS, connection reset, timeout) or when the failure was local -- a
    field, so callers can tell "the tag is absent" from "the registry did not
    answer" without re-parsing the message.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _get(url: str, token: str | None = None, accept: tuple[str, ...] = ()) -> bytes:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if accept:
        headers["Accept"] = ", ".join(accept)
    try:
        with urlopen(Request(url, headers=headers), timeout=_TIMEOUT_S) as resp:
            return resp.read()
    except HTTPError as exc:
        raise OciError(f"{url} -> HTTP {exc.code} {exc.reason}", status=exc.code) from exc
    except (URLError, OSError) as exc:
        raise OciError(f"{url} -> {exc}") from exc


def repo_path(image: str) -> str:
    """`ghcr.io/<org>/<name>` -> `<org>/<name>`, the form the v2 API wants."""
    if "/" not in image:
        raise OciError(f"{image!r} names no registry; expected something like ghcr.io/<org>/<name>")
    return image.split("/", 1)[1]


def split_ref(image: str) -> tuple[str, str]:
    """`<registry>/<repo>[:tag][@sha256:...]` -> the v2 repo path and the reference.

    A digest ref splits on `@`, never on the last colon: rpartition(":") on
    `...-sim-assets@sha256:<hex>` yields the repository `...-sim-assets@sha256`,
    which no registry has, so the token scope and the manifest URL both name a
    package that does not exist and GHCR answers an opaque 401/404. Colons are
    only tags when they fall in the last path element -- one before it is a
    registry port. `<repo>:tag@sha256:...` pins by digest, so the tag is dropped.
    """
    name, at, digest = image.partition("@")
    tail = name.rpartition("/")[2]
    tag = tail.rpartition(":")[2] if ":" in tail else ""
    if tag:
        name = name[: -len(tag) - 1]
    reference = digest if at else tag
    if not reference:
        raise OciError(f"{image!r} names no tag or digest; expected <registry>/<org>/<name>:<tag> or ...@sha256:<hex>")
    return repo_path(name), reference


def validate_layer_member(member: tarfile.TarInfo) -> None:
    """Shared install/publication contract: safe paths, regular files and dirs."""
    if member.name.startswith(("/", "..")) or ".." in Path(member.name).parts:
        raise OciError(f"unsafe path in asset layer: {member.name}")
    if not (member.isfile() or member.isdir()):
        raise OciError(f"unsupported member type in asset layer: {member.name}")


def safe_extract(blob: Path, dest: Path) -> None:
    """Extract a layer tarball, rejecting anything that could escape `dest`.

    The blob is digest-verified, but the LOCK chooses that digest, so the
    archive is still untrusted input. `filter="data"` would cover these checks
    in one pass, but it does not exist on 3.10, which this must run on.

    Validated lazily, as a generator, so the archive is decompressed ONCE:
    `getmembers()` walks the whole gzip stream and leaves the file at EOF,
    after which `extractall` seeks back to 0 and GzipFile restarts -- about a
    second of waste per ~100 MB layer. Members are still checked before
    tarfile is handed them, so a rejection can only leave earlier, safe members
    on disk, and every caller stages into a directory it removes on failure.
    """

    def validated(tar: tarfile.TarFile):
        for member in tar:
            validate_layer_member(member)
            yield member

    with tarfile.open(blob) as tar:
        tar.extractall(dest, members=validated(tar))


def anon_token(repo: str) -> str:
    """Anonymous pull token. Public packages need no credentials, but GHCR still
    requires a bearer token rather than accepting unauthenticated requests."""
    url = f"https://{REGISTRY}/token?scope=repository:{repo}:pull&service={REGISTRY}"
    try:
        return json.loads(_get(url))["token"]
    except (KeyError, json.JSONDecodeError) as exc:
        raise OciError(f"malformed token response from {url}") from exc


def host_arch() -> str | None:
    """The container architecture for this host's CPU, or None if unrecognized.

    The one machine->arch table, shared with runtime._docker_platform, so
    manifest selection and pull refusal cannot diverge.
    """
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "amd64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    return None


def fetch_manifest(repo: str, ref: str, token: str) -> dict:
    """The image manifest for `ref`, resolving a multi-arch index to this host.

    `ref` is a tag or a sha256: digest. Falls back to the first entry when no
    platform matches -- the asset image is pure data, so any arch's manifest
    lists the same layers.
    """
    url = f"https://{REGISTRY}/v2/{repo}/manifests/{ref}"
    doc = json.loads(_get(url, token, _INDEX_TYPES + _MANIFEST_TYPES))
    if "layers" in doc:
        return doc

    entries = doc.get("manifests") or []
    if not entries:
        raise OciError(f"{repo}:{ref} has neither layers nor manifests")
    # An unrecognized machine falls back to amd64, for the same reason.
    want_os, want_arch = "linux", host_arch() or "amd64"
    chosen = next(
        (
            m
            for m in entries
            if m.get("platform", {}).get("os") == want_os and m.get("platform", {}).get("architecture") == want_arch
        ),
        entries[0],
    )
    return json.loads(_get(f"https://{REGISTRY}/v2/{repo}/manifests/{chosen['digest']}", token, _MANIFEST_TYPES))


def manifest_for_image(image: str) -> dict:
    """The manifest the registry serves for a full `<registry>/<repo>:<ref>`.

    Raises OciError for every way this can fail, json.JSONDecodeError included:
    a truncated or non-JSON body is a registry that did not serve the manifest,
    not a programming error, so "no manifest" stays one except clause.
    """
    repo, reference = split_ref(image)
    try:
        return fetch_manifest(repo, reference, anon_token(repo))
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise OciError(f"{image} -> malformed manifest response: {exc}") from exc


def _report_download(label: str, done: int, total: int, *, live: bool) -> None:
    """One progress line: the enclosing step's detail when there is one, a bar
    in place on a bare terminal, an occasional log line otherwise (silence
    reads as a hang on a cold ~85 MB download)."""
    bar = render_progress_bar(done / total if total else 0.0)
    size = f"{format_bytes(done)} / {format_bytes(total)}" if total else format_bytes(done)
    step = active_step()
    if step is not None:
        step.detail = f"{bar} {size}"
        return
    if not live:
        log(f"Downloading {label}... {format_bytes(done)}" + (f" of {format_bytes(total)}" if total else ""))
        return
    print(f"\r\033[K  {bar} {size}  {DIM}{label}{NC}", end="", flush=True)


def fetch_layer(repo: str, digest: str, dest, token: str, *, label: str = "layer") -> None:
    """Stream one layer blob to `dest` (an open binary file), verifying `digest`.

    Reports progress on the same 5 s cadence as the rest of the launcher, since
    this is an ~85 MB download on a cold cache and silence reads as a hang.
    """
    if not digest.startswith("sha256:"):
        raise OciError(f"unsupported digest algorithm: {digest}")

    url = f"https://{REGISTRY}/v2/{repo}/blobs/{digest}"
    sha = hashlib.sha256()
    headers = {"Authorization": f"Bearer {token}"}
    live = sys.stdout.isatty()
    try:
        with urlopen(Request(url, headers=headers), timeout=_TIMEOUT_S) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            next_report = time.monotonic()
            while chunk := resp.read(1 << 20):
                sha.update(chunk)
                dest.write(chunk)
                done += len(chunk)
                if time.monotonic() >= next_report:
                    _report_download(label, done, total, live=live)
                    next_report = time.monotonic() + (0.5 if live else 5.0)
            if live and active_step() is None:
                _report_download(label, done, total or done, live=True)
                print()
    except HTTPError as exc:
        raise OciError(f"{url} -> HTTP {exc.code} {exc.reason}", status=exc.code) from exc
    except (URLError, OSError) as exc:
        raise OciError(f"Failed to download {label} from {url}: {exc}") from exc

    got = f"sha256:{sha.hexdigest()}"
    if got != digest:
        raise OciError(f"{label} digest mismatch (got {got}, manifest says {digest})")
