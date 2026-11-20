"""Getting a node ready for presets: what it has, and fetching what it lacks.

inventory()  every model file this ComfyUI can load, and the GPU's memory
fetch()      downloads the files the chosen presets need from the broker's
             catalog, checks each against its sha256, and puts it in the right
             models/ folder, within a disk budget
blender()    finds Blender, or fetches the portable Linux build the catalog names
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import platform
import shutil
import tarfile

import httpx

log = logging.getLogger("snapfire.node")

EXTENSIONS = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".sft")
HEADROOM = 10 * 2 ** 30  # always leave this much disk free


def _options(spec: list) -> list:
    if spec and isinstance(spec[0], list):
        return spec[0]  # old style: [["a.safetensors", ...], {...}]
    if len(spec) > 1 and isinstance(spec[1], dict):
        return spec[1].get("options") or []  # new style: ["COMBO", {"options": [...]}]
    return []


async def inventory(comfy: httpx.AsyncClient) -> tuple[set[str], int | None]:
    """Model files any loader in this ComfyUI offers, and total VRAM in MB."""
    info = (await comfy.get("/object_info", timeout=120)).json()
    files: set[str] = set()
    for node in info.values():
        for section in ("required", "optional"):
            for spec in ((node.get("input") or {}).get(section) or {}).values():
                if isinstance(spec, list):
                    files |= {o for o in _options(spec) if isinstance(o, str) and o.lower().endswith(EXTENSIONS)}
    vram = None
    try:
        dev = (await comfy.get("/system_stats")).json()["devices"][0]
        vram = int(dev["vram_total"]) // 2 ** 20
    except (httpx.HTTPError, KeyError, IndexError, ValueError):
        pass
    return files, vram


def wanted(presets: list[dict], chosen: set[str]) -> list[dict]:
    return [p for p in presets if "all" in chosen or p["id"] in chosen]


async def download(client: httpx.AsyncClient, url: str, dest: str, sha256: str, size: int) -> None:
    """Resumes a .part file, checks the size and sha256, then moves it into place."""
    part = dest + ".part"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    have = os.path.getsize(part) if os.path.exists(part) else 0
    if have < size:
        headers = {"Range": f"bytes={have}-"} if have else {}
        async with client.stream("GET", url, headers=headers, follow_redirects=True, timeout=None) as r:
            if r.status_code == 200 and have:
                have = 0  # the server ignored the range: start over
            r.raise_for_status()
            next_note = have + size // 10
            with open(part, "ab" if have else "wb") as f:
                async for chunk in r.aiter_bytes(1 << 20):
                    f.write(chunk)
                    have += len(chunk)
                    if have >= next_note:
                        log.info("  %s: %d%%", os.path.basename(dest), have * 100 // size)
                        next_note += size // 10
    if os.path.getsize(part) != size:
        os.remove(part)
        raise RuntimeError(f"{os.path.basename(dest)}: wrong size, deleted; it will be fetched again")
    digest = await asyncio.to_thread(_sha256, part)
    if digest != sha256:
        os.remove(part)
        raise RuntimeError(f"{os.path.basename(dest)}: checksum does not match the catalog, deleted")
    os.replace(part, dest)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 22):
            h.update(chunk)
    return h.hexdigest()


async def fetch(broker: httpx.AsyncClient, have: set[str], chosen: set[str], models_dir: str,
                budget_gb: float) -> list[str]:
    """Download the files the chosen presets need and this node lacks. Returns what was fetched."""
    presets = (await broker.get("/presets")).json()["presets"]
    catalog = (await broker.get("/catalog")).json()["models"]
    picked = wanted(presets, chosen)
    unknown = chosen - {p["id"] for p in presets} - {"all"}
    if unknown:
        log.warning("no such preset on this broker: %s", ", ".join(sorted(unknown)))
    budget = int(budget_gb * 2 ** 30)
    fetched: list[str] = []
    async with httpx.AsyncClient() as dl:
        for p in picked:
            missing = [f for f in p["needs"]["files"] if f not in have and f not in fetched]
            if not missing:
                continue
            not_listed = [f for f in missing if f not in catalog]
            if not_listed:
                log.warning("%s: can't fetch %s (not in the catalog); put it in models/ yourself",
                            p["name"], ", ".join(not_listed))
                continue
            size = sum(catalog[f]["size"] for f in missing)
            free = shutil.disk_usage(models_dir).free if os.path.isdir(models_dir) else 0
            if size > budget or size > free - HEADROOM:
                log.warning("%s: needs %.1f GB more, over the budget (%.1f GB left) or the free disk (%.1f GB); "
                            "skipped", p["name"], size / 2 ** 30, budget / 2 ** 30, (free - HEADROOM) / 2 ** 30)
                continue
            log.info("%s: fetching %d file(s), %.1f GB", p["name"], len(missing), size / 2 ** 30)
            for f in missing:
                m = catalog[f]
                try:
                    await download(dl, m["url"], os.path.join(models_dir, m["folder"], f), m["sha256"], m["size"])
                except (httpx.HTTPError, OSError, RuntimeError) as e:
                    log.warning("%s: %s", f, e)
                    break
                fetched.append(f)
                budget -= m["size"]
                log.info("  %s ready (%s, %s)", f, m["folder"], m["license"])
    return fetched


async def blender(broker: httpx.AsyncClient, setting: str, tools_dir: str) -> str:
    """BLENDER=<path> is used as is. BLENDER=auto finds an installed Blender, or on Linux x64
    fetches the portable build the broker's catalog names into tools_dir."""
    if setting != "auto":
        return setting
    for path in ("/Applications/Blender.app/Contents/MacOS/Blender", shutil.which("blender") or ""):
        if path and os.access(path, os.X_OK):
            return path
    prog = (await broker.get("/catalog")).json()["programs"]["blender"]
    exe = os.path.join(tools_dir, prog["binary"])
    if os.access(exe, os.X_OK):
        return exe
    if f"{platform.system().lower()}-{platform.machine()}" != prog["platform"]:
        log.warning("BLENDER=auto: no Blender found and no portable build for this system; "
                    "install Blender and set BLENDER to its path")
        return ""
    log.info("fetching Blender %s (%.0f MB, %s)", prog["version"], prog["size"] / 2 ** 20, prog["license"])
    archive = os.path.join(tools_dir, os.path.basename(prog["url"]))
    async with httpx.AsyncClient() as dl:
        await download(dl, prog["url"], archive, prog["sha256"], prog["size"])
    await asyncio.to_thread(_extract, archive, tools_dir)
    os.remove(archive)
    return exe if os.access(exe, os.X_OK) else ""


def _extract(archive: str, dest: str) -> None:
    with tarfile.open(archive) as t:
        t.extractall(dest, filter="tar")
