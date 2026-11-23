"""Download the local models the assistant needs (`octavia setup`).

Each provider declares its assets; this module fetches them with progress,
verifies checksums where known, and unpacks archives. Downloads go to a temp
file first so an interrupted download never leaves a half-written model.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from octavia.config import AppConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    dest: str  # file path, or the directory to extract into when ``archive`` is set
    sha256: str | None = None
    archive: str | None = None  # "tar.bz2" | "tar.gz" | "zip"
    strip_top_dir: bool = True  # drop the archive's single top-level folder when extracting
    marker: str | None = None  # file inside dest that proves an extracted archive is complete


def asset_present(asset: Asset, config: AppConfig) -> bool:
    dest = config.resolve_path(asset.dest)
    if asset.archive:
        return (dest / asset.marker).exists() if asset.marker else dest.is_dir() and any(dest.iterdir())
    return dest.is_file() and dest.stat().st_size > 0


def required_assets(config: AppConfig) -> list[Asset]:
    from octavia.vad.silero import MODEL_SHA256, MODEL_URL

    assets = [Asset("Silero VAD", MODEL_URL, config.vad.model_path, sha256=MODEL_SHA256)]
    if config.vad.smart_turn_model:
        from octavia.vad import smart_turn

        assets.append(
            Asset("Smart Turn", smart_turn.MODEL_URL, config.vad.smart_turn_model, sha256=smart_turn.MODEL_SHA256)
        )
    try:
        from octavia.wake import required_assets as wake_assets

        assets += wake_assets(config)
    except ImportError:
        pass
    try:
        from octavia.stt import required_assets as stt_assets

        assets += stt_assets(config)
    except ImportError:
        pass
    return assets


def _download(url: str, target: Path) -> str:
    import httpx

    from octavia.net import tls_context

    digest = hashlib.sha256()
    with httpx.stream(
        "GET", url, follow_redirects=True, timeout=httpx.Timeout(60.0, connect=15.0), verify=tls_context()
    ) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        done = 0
        with target.open("wb") as f:
            for chunk in r.iter_bytes(1 << 16):
                f.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if total and sys.stdout is not None:
                    sys.stdout.write(f"\r    {done / 1e6:7.1f} / {total / 1e6:.1f} MB")
                    sys.stdout.flush()
    if total and sys.stdout is not None:
        sys.stdout.write("\n")
    return digest.hexdigest()


def _extract(archive: Path, kind: str, dest: Path, strip_top_dir: bool) -> None:
    staging = Path(tempfile.mkdtemp(dir=dest.parent, prefix=".extract-"))
    try:
        if kind.startswith("tar"):
            mode = "r:bz2" if kind == "tar.bz2" else "r:gz"
            with tarfile.open(archive, mode) as tar:
                tar.extractall(staging, filter="data")
        elif kind == "zip":
            with zipfile.ZipFile(archive) as z:
                for member in z.namelist():  # zip-slip guard
                    if Path(member).is_absolute() or ".." in Path(member).parts:
                        raise ValueError(f"unsafe path in archive: {member}")
                z.extractall(staging)
        else:
            raise ValueError(f"unknown archive type {kind}")
        root = staging
        children = list(staging.iterdir())
        if strip_top_dir and len(children) == 1 and children[0].is_dir():
            root = children[0]
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(root), str(dest))
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def fetch(asset: Asset, config: AppConfig, force: bool = False) -> bool:
    dest = config.resolve_path(asset.dest)
    if not force and asset_present(asset, config):
        print(f"  ok   {asset.name} ({asset.dest})")
        return True
    print(f"  get  {asset.name}  <- {asset.url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=".download-")
    tmp = Path(tmp_name)
    try:
        with open(fd, "wb"):
            pass
        digest = _download(asset.url, tmp)
        if asset.sha256 and digest.lower() != asset.sha256.lower():
            print(f"  FAIL {asset.name}: checksum mismatch (got {digest})")
            return False
        if asset.archive:
            _extract(tmp, asset.archive, dest, asset.strip_top_dir)
        else:
            tmp.replace(dest)
        print(f"  done {asset.name}")
        return True
    except Exception as exc:
        log.exception("Download failed: %s", asset.name)
        print(f"  FAIL {asset.name}: {exc}")
        return False
    finally:
        tmp.unlink(missing_ok=True)


async def download_all(config: AppConfig, force: bool = False) -> bool:
    import asyncio

    print("Downloading local models into", config.resolve_path("models"))
    ok = True
    for asset in required_assets(config):
        ok = await asyncio.to_thread(fetch, asset, config, force) and ok
    try:
        from octavia.stt import prefetch as stt_prefetch
    except ImportError:
        stt_prefetch = None
    if stt_prefetch is not None:
        ok = await asyncio.to_thread(stt_prefetch, config) and ok
    from octavia.emotion import prefetch as emotion_prefetch  # the voice tone's model (emotion.enabled)

    ok = await asyncio.to_thread(emotion_prefetch, config) and ok
    print("All models ready." if ok else "Some downloads failed; see above and data/logs/octavia.log.")
    return ok
