"""Snapfire node worker: pull tasks from the broker, run them locally.

Text tasks run on a local Ollama. ComfyUI tasks run on a local ComfyUI server.
One worker can serve either or both. It dials out to the broker, so it works
behind NAT, and it never stores prompts, workflows or outputs.

Env:
  BROKER_URL       broker base url                    (http://localhost:8400)
  PROVIDER_NAME    display name                       (provider)
  PROVIDER_KEY     existing api key; a provider account is created if unset
  CONCURRENCY      tasks run in parallel              (1)

  OLLAMA_URL       local ollama; empty to serve no text models   (http://localhost:11434)
  TEXT_MODEL       text model to serve                (qwen2.5:1.5b)
  PRICE_PER_1K     micro-dollars per 1k tokens        (200)
  MODELS           several models with a price each, e.g. "qwen2.5:1.5b=200,qwen2.5:7b=600"
                   (replaces TEXT_MODEL and PRICE_PER_1K)

  COMFY_URL        local ComfyUI server, e.g. http://localhost:8188; unset to serve no ComfyUI work
  COMFY_PRICE      your floor, in micro-dollars per megapixel-step: no image work below it.
                   Unset (the usual): worked out from what your GPU's electricity costs per unit,
                   measured on the jobs it runs. Everyone is paid Snapfire's market price.
  GPU_WATTS        your GPU's power draw, for that floor (read from nvidia-smi; 60 on a Mac)
  KWH_PRICE        what your electricity costs, in dollars per kWh   (0.15)
  GPU_NAME         your graphics card, if ComfyUI can't tell (a Mac's chip, e.g. "Apple M4 Pro")
  COMFY_MODELS     only offer these checkpoint/diffusion-model files, comma separated (default: all)
  GPU_NEED_MB      share the GPU: take no work while other programs leave less than this much VRAM (0: off)

  BLENDER          path to a blender binary, or "auto" (find one, or fetch the portable Linux build)
  TOOL_PRICE       micro-dollars per tool run         (2000)

  PRESETS          presets to serve, comma separated, or "all" (see GET /presets on the broker).
                   Limits what the node offers to what those presets use.
  FETCH_MODELS     1: download the model files those presets need and this ComfyUI lacks  (0)
  COMFY_MODELS_DIR ComfyUI's models/ folder, where fetched files go
  DISK_BUDGET_GB   most to download in all                         (100)
  TOOLS_DIR        where BLENDER=auto puts Blender                 (~/.snapfire-tools)
  REPORT_FILES     "presets": tell the broker only about files the chosen presets use;
                   "all": every model file (lets uploaded workflows find this node)
                   (default: presets when PRESETS is set, else all)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import math
import shutil
import subprocess
import tempfile
import time
import uuid

import httpx

from . import prepare
from .progress import Reporter, Stopped, Tracker, listen

log = logging.getLogger("snapfire.node")

BROKER = os.environ.get("BROKER_URL", "http://localhost:8400")
NAME = os.environ.get("PROVIDER_NAME", "provider")
CONCURRENCY = int(os.environ.get("CONCURRENCY", "1"))

OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL = os.environ.get("TEXT_MODEL", "qwen2.5:1.5b")
PRICE = int(os.environ.get("PRICE_PER_1K", "200"))
LLM_MODELS = {m.strip(): int(p) for m, p in
              (pair.rsplit("=", 1) for pair in os.environ.get("MODELS", "").split(",") if pair.strip())} \
    or ({MODEL: PRICE} if OLLAMA else {})

COMFY = os.environ.get("COMFY_URL", "")
# The floor for image work: a fixed one if the owner set COMFY_PRICE, otherwise learned (see Floors).
COMFY_PRICE_SET = os.environ.get("COMFY_PRICE", "").strip()
COMFY_PRICE = int(COMFY_PRICE_SET) if COMFY_PRICE_SET else 10  # before anything is measured: a quarter of the starting price
KWH_PRICE = float(os.environ.get("KWH_PRICE", "0.15"))
FLOOR_MARGIN = 1.25
GPU_NEED_MB = int(os.environ.get("GPU_NEED_MB", "0"))
OOM_RETRIES = 3
COMFY_ONLY = {m.strip() for m in os.environ.get("COMFY_MODELS", "").split(",") if m.strip()}
# A file the broker keeps is named "<prefix>-file:<id>" in tasks; the node only needs the id.
FILE_REF = re.compile(r"^[a-z]+-file:([A-Za-z0-9_-]+)$")


def file_id(v) -> str | None:
    m = FILE_REF.match(v) if isinstance(v, str) else None
    return m.group(1) if m else None
BLENDER = os.environ.get("BLENDER", "")
PRESETS = {p.strip() for p in os.environ.get("PRESETS", "").split(",") if p.strip()}
FETCH_MODELS = os.environ.get("FETCH_MODELS", "0") == "1"
COMFY_MODELS_DIR = os.environ.get("COMFY_MODELS_DIR", "")
DISK_BUDGET_GB = float(os.environ.get("DISK_BUDGET_GB", "100"))
BLENDER_HOME = os.path.expanduser(os.environ.get("TOOLS_DIR", "~/.snapfire-tools"))  # where BLENDER=auto puts it
REPORT_FILES = os.environ.get("REPORT_FILES", "presets" if PRESETS else "all")
NODE = {"files": None, "vram_mb": None, "gpu": None}  # what this node has, as last reported
# new on every start: tells the broker this is a new process, so tasks handed to the old one go back in the queue
BOOT = uuid.uuid4().hex[:8]
TOOL_PRICE = int(os.environ.get("TOOL_PRICE", "2000"))
SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools")  # the Blender scripts this node ships
TOOL_TIMEOUT = 600
CLIENT_ID = uuid.uuid4().hex

MODELS: dict[str, int] = dict(LLM_MODELS)  # everything this worker offers; ComfyUI keys are added at startup

SYSTEM = "You are a helpful assistant. Follow the task exactly."
SYSTEM_JSON = SYSTEM + " Reply with JSON that matches the given schema and nothing else."


# ---- text tasks on Ollama ---------------------------------------------------

async def run_ollama(ollama: httpx.AsyncClient, task: dict) -> dict:
    schema = task.get("output_schema")
    model = task.get("model") or MODEL
    if model not in LLM_MODELS:
        raise RuntimeError(f"this provider does not serve {model}")
    body = {
        "model": model,
        "stream": False,
        "messages": [{"role": "system", "content": SYSTEM_JSON if schema else SYSTEM},
                     {"role": "user", "content": task["prompt"] +
                      (f"\n\nJSON schema:\n{json.dumps(schema)}" if schema else "")}],
        "options": {"temperature": task.get("temperature", 0.2), "num_predict": task.get("max_tokens", 512)},
    }
    if schema:
        body["format"] = schema  # Ollama constrains decoding to the schema
    r = await ollama.post("/api/chat", json=body)
    r.raise_for_status()
    data = r.json()
    text = data["message"]["content"]
    output = json.loads(text) if schema else text
    return {"output": output, "prompt_tokens": data.get("prompt_eval_count", 0),
            "completion_tokens": data.get("eval_count", 0), "model": model}


async def ensure_models(ollama: httpx.AsyncClient) -> None:
    tags = (await ollama.get("/api/tags")).json().get("models", [])
    have = {m["name"] for m in tags}
    for model in LLM_MODELS:
        if model in have or f"{model}:latest" in have:
            continue
        log.info("pulling %s (first run only)...", model)
        r = await ollama.post("/api/pull", json={"model": model, "stream": False}, timeout=None)
        r.raise_for_status()
        log.info("pulled %s", model)


# ---- ComfyUI tasks ----------------------------------------------------------

async def comfy_checkpoints(comfy: httpx.AsyncClient) -> list[str]:
    """Checkpoint and diffusion-model files this ComfyUI has, which decide the workflows it can take."""
    names: list[str] = []
    for node, field in (("CheckpointLoaderSimple", "ckpt_name"), ("UNETLoader", "unet_name")):
        info = (await comfy.get(f"/object_info/{node}")).json()
        if node not in info:
            continue
        spec = info[node]["input"]["required"][field]
        names += spec[0] if isinstance(spec[0], list) else (spec[1] or {}).get("options", [])
    return [n for n in names if isinstance(n, str)]


VIDEO_EXT = (".mp4", ".webm", ".mov", ".mkv")
# a customer's input file goes to ComfyUI under the extension its loader looks for
# (LoadVideo lists video files, LoadAudio audio files)
INPUT_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "video/mp4": ".mp4",
             "video/webm": ".webm", "video/quicktime": ".mov", "audio/wav": ".wav", "audio/x-wav": ".wav",
             "audio/wave": ".wav", "audio/mpeg": ".mp3", "audio/flac": ".flac", "audio/x-flac": ".flac",
             "audio/ogg": ".ogg", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a"}
CONTENT_TYPES = {".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
                 ".latent": "application/octet-stream", ".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp", ".glb": "model/gltf-binary",
                 ".fbx": "application/octet-stream", ".json": "application/json"}


def content_type(name: str) -> str:
    return CONTENT_TYPES.get(os.path.splitext(name)[1].lower(), "application/octet-stream")


async def hand_in(broker: httpx.AsyncClient, task: dict, name: str, data: bytes) -> dict:
    up = await broker.post(f"/provider/tasks/{task['id']}/files", params={"name": name}, content=data,
                           headers={"Content-Type": content_type(name)})
    up.raise_for_status()
    return up.json()


async def run_comfy(comfy: httpx.AsyncClient, broker: httpx.AsyncClient, task: dict) -> dict:
    wf = task["workflow"]
    report = Reporter(broker, task["id"])
    # An input file (an upload, or what an earlier stage made) arrives as a file reference: fetch it, give it to ComfyUI.
    for node in wf.values():
        for key, v in node.get("inputs", {}).items():
            fid = file_id(v)
            if fid:
                await report(0.01, "Fetching the input files", now=True)
                src = await broker.get(f"/files/{fid}")
                src.raise_for_status()
                # a latent from an earlier stage (LoadLatent only lists files that end in .latent)
                ctype = src.headers.get("content-type", "image/png").split(";")[0].strip()
                ext = ".latent" if node.get("class_type") == "LoadLatent" else INPUT_EXT.get(ctype, ".png")
                name = f"sf_{fid[:16]}{ext}"
                up = await comfy.post("/upload/image", data={"overwrite": "true"},
                                      files={"image": (name, src.content, src.headers.get("content-type", "image/png"))})
                up.raise_for_status()
                node["inputs"][key] = up.json()["name"]

    # A different model than last time: unload the old ones first. ComfyUI keeps models in RAM after a
    # run, and big ones back to back (an image-edit model, then a video model) can run the machine out of
    # memory, which kills ComfyUI mid-job.
    model = task.get("model") or ""
    if LAST_MODEL["name"] and model != LAST_MODEL["name"]:
        await report(0.01, "Making room for the model", now=True)
        try:
            await comfy.post("/free", json={"unload_models": True, "free_memory": True})
            await asyncio.sleep(2)
        except httpx.HTTPError:
            pass
    LAST_MODEL["name"] = model

    for attempt in range(OOM_RETRIES + 1):
        try:
            entry = await comfy_once(comfy, wf, report)
            break
        except OutOfMemory as e:
            if attempt == OOM_RETRIES:
                raise RuntimeError(f"ComfyUI ran out of GPU memory {OOM_RETRIES + 1} times: {e}")
            log.info("out of GPU memory, freeing ComfyUI's models and trying again in 30 s")
            await report(0.02, "The GPU ran out of memory: freeing it and trying again", now=True)
            await comfy.post("/free", json={"unload_models": True, "free_memory": True})
            await asyncio.sleep(30)

    await report(0.97, "Sending back the result", now=True)
    images, meshes, latents, videos = [], [], [], []
    for out in entry.get("outputs", {}).values():
        for kind, found in (("images", images), ("3d", meshes), ("latents", latents)):
            for f in out.get(kind) or []:
                if not isinstance(f, dict) or not f.get("filename") or f.get("type", "output") != "output":
                    continue  # previews are not results
                view = await comfy.get("/view", params={"filename": f["filename"],
                                                        "subfolder": f.get("subfolder", ""), "type": "output"},
                                       timeout=600)
                view.raise_for_status()
                ref = await hand_in(broker, task, f["filename"], view.content)
                meta = f.get("metadata") or {}
                # ComfyUI lists a saved video among the images, marked as a video
                if kind == "images" and (meta.get("kind") == "video" or f["filename"].lower().endswith(VIDEO_EXT)):
                    videos.append({**ref, **{k: meta[k] for k in ("width", "height", "duration", "fps") if k in meta}})
                else:
                    found.append(ref)
    if not images and not meshes and not videos:
        raise RuntimeError("the workflow produced no images, videos or models")
    output: dict = {}
    if videos:
        output.update(video=videos[0]["file"], videos=videos)
    if images:
        output.update(image=images[0]["file"], images=images)
    if meshes:
        output.update(mesh=meshes[0]["file"], meshes=meshes)
    if latents:  # a draft, to be finished by a later stage
        output.update(latent=latents[0]["file"], latents=latents)
    return {"output": output, "model": task["model"]}


class OutOfMemory(RuntimeError):
    pass


async def comfy_once(comfy: httpx.AsyncClient, wf: dict, report: Reporter) -> dict:
    """Run a workflow, telling the broker how far it is as ComfyUI says so on its WebSocket."""
    tracker, prompt, connected = Tracker(wf), {}, asyncio.Event()
    listener = asyncio.create_task(listen(COMFY, CLIENT_ID, tracker, prompt, connected))
    try:
        try:
            await asyncio.wait_for(connected.wait(), 6)  # listen before the run starts, to miss nothing
        except asyncio.TimeoutError:
            pass
        r = await comfy.post("/prompt", json={"prompt": wf, "client_id": CLIENT_ID})
        if r.status_code != 200:
            raise RuntimeError(f"ComfyUI rejected the workflow: {r.text[:400]}")
        prompt_id = prompt["id"] = r.json()["prompt_id"]
        while True:
            await asyncio.sleep(0.5)
            try:
                await report(0.03 + 0.92 * tracker.percent, tracker.label)
            except Stopped:
                await stop_prompt(comfy, prompt_id)
                raise
            entry = (await comfy.get(f"/history/{prompt_id}")).json().get(prompt_id)
            if not entry:
                continue
            status = entry.get("status") or {}
            if status.get("status_str") == "error":
                err = next((m[1] for m in status.get("messages", []) if m[0] == "execution_error"), {})
                what = f"{err.get('node_type', '?')}: {err.get('exception_type', '')} {str(err.get('exception_message', ''))[:300]}"
                if "OutOfMemory" in str(err.get("exception_type")) or "out of memory" in what:
                    raise OutOfMemory(what)
                raise RuntimeError(f"ComfyUI failed in {what}")
            if status.get("completed", True) or entry.get("outputs"):
                return entry
    finally:
        listener.cancel()


async def stop_prompt(comfy: httpx.AsyncClient, prompt_id: str) -> None:
    """Break off our run only: interrupt it if it is the one running, else take it out of the queue."""
    try:
        q = (await comfy.get("/queue")).json()
        if any(len(e) > 1 and e[1] == prompt_id for e in q.get("queue_running", [])):
            await comfy.post("/interrupt", json={"prompt_id": prompt_id})
        else:
            await comfy.post("/queue", json={"delete": [prompt_id]})
    except httpx.HTTPError as e:
        log.warning("couldn't stop the run in ComfyUI: %s", e)


async def gpu_room(comfy: httpx.AsyncClient) -> int | None:
    """Free VRAM in MB, not counting what ComfyUI's allocator reports holding. With
    cudaMallocAsync that report leaves out cached models, so a low number may be
    ComfyUI itself: wait_for_gpu frees its cache before deciding."""
    try:
        dev = (await comfy.get("/system_stats")).json()["devices"][0]
    except (httpx.HTTPError, KeyError, IndexError, ValueError):
        return None
    total, free, mine = dev["vram_total"], dev["vram_free"], dev.get("torch_vram_total", 0)
    others = max(0, total - free - mine)
    return (total - others) // 2**20


GPU_STATE = {"paused": False, "checked": 0.0}
LAST_MODEL = {"name": ""}  # the model of the last ComfyUI run, to unload it when the next run needs another


async def gpu_free_enough(comfy: httpx.AsyncClient) -> bool:
    room = await gpu_room(comfy)
    if room is not None and room < GPU_NEED_MB:  # maybe it's ComfyUI's own model cache
        try:
            await comfy.post("/free", json={"unload_models": True, "free_memory": True})
            await asyncio.sleep(3)
        except httpx.HTTPError:
            pass
        room = await gpu_room(comfy)
    if room is not None and room < GPU_NEED_MB and not GPU_STATE["paused"]:
        log.info("GPU busy with other programs (%d MB left, need %d): pausing ComfyUI work", room, GPU_NEED_MB)
    return room is None or room >= GPU_NEED_MB


async def wait_for_gpu(comfy: httpx.AsyncClient, broker: httpx.AsyncClient) -> None:
    """Share the GPU. While other programs leave too little VRAM, tell the broker the ComfyUI
    models are paused: the node stays listed (jobs are priced and queued for it) and keeps
    taking work that needs no GPU, like Blender tools. Checked at most every 15 s."""
    if not GPU_NEED_MB or time.monotonic() - GPU_STATE["checked"] < 15:
        return
    GPU_STATE["checked"] = time.monotonic()
    busy = not await gpu_free_enough(comfy)
    if busy != GPU_STATE["paused"]:
        if not busy:
            log.info("GPU free again, taking ComfyUI work")
        GPU_STATE["paused"] = busy
        await register(broker, quiet=True)


# ---- tools: fixed programs shipped with the node ----------------------------

NODE_TOOLS = ("blender.rig_ready", "blender.turntable")  # programs in tools/ this node runs with Blender


async def run_turntable(broker: httpx.AsyncClient, task: dict) -> dict:
    args = task["tool"]["args"]
    mesh, seconds, bg = str(args["mesh"]), float(args["seconds"]), str(args["background"])
    if not file_id(mesh) or not 2 <= seconds <= 12 or bg not in ("dark", "light"):
        raise RuntimeError("bad tool arguments")
    report = Reporter(broker, task["id"])
    await report(0.05, "Fetching the model", now=True)
    src = await broker.get(f"/files/{file_id(mesh)}", timeout=300)
    src.raise_for_status()
    await report(0.15, "Blender: rendering the turntable", now=True)
    work = tempfile.mkdtemp(prefix="snapfire-tool-")
    try:
        with open(os.path.join(work, "in.glb"), "wb") as f:
            f.write(src.content)
        out = os.path.join(work, "out")
        proc = await asyncio.create_subprocess_exec(
            BLENDER, "-b", "--factory-startup", "--python-exit-code", "1",
            "-P", os.path.join(SCRIPTS, "turntable.py"), "--", os.path.join(work, "in.glb"), out, str(seconds), bg,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            log_out, _ = await asyncio.wait_for(proc.communicate(), TOOL_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("blender took too long")
        if proc.returncode != 0:
            raise RuntimeError("blender failed: " + log_out.decode(errors="replace")[-400:])
        await report(0.85, "Making the video", now=True)
        frames = sorted(os.listdir(os.path.join(out, "frames")))
        video = os.path.join(out, "turntable.mp4")
        enc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-v", "error", "-y", "-framerate", "24", "-i", os.path.join(out, "frames", "f_%04d.png"),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", "-movflags", "+faststart", video)
        if await enc.wait() != 0:
            raise RuntimeError("ffmpeg failed to make the video")
        with open(video, "rb") as f:
            vid = await hand_in(broker, task, "turntable.mp4", f.read())
        with open(os.path.join(out, "frames", frames[len(frames) // 8]), "rb") as f:
            poster = await hand_in(broker, task, "turntable.png", f.read())
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {"output": {"video": vid["file"], "videos": [{**vid, "width": 720, "height": 720, "fps": 24.0,
                                                          "duration": round(len(frames) / 24, 2)}],
                       "image": poster["file"], "images": [poster]}, "model": task["model"]}


async def run_tool(broker: httpx.AsyncClient, task: dict) -> dict:
    tool = task["tool"]
    if tool["name"] not in NODE_TOOLS or not BLENDER:
        raise RuntimeError(f"this provider does not run {tool['name']}")
    if tool["name"] == "blender.turntable":
        return await run_turntable(broker, task)
    args = tool["args"]
    mesh = str(args["mesh"])
    if not file_id(mesh):
        raise RuntimeError("mesh must be a file from the broker")
    height, pose = float(args["height"]), str(args["pose"])
    if pose not in ("T", "A") or not 0.05 <= height <= 20:
        raise RuntimeError("bad tool arguments")
    report = Reporter(broker, task["id"])
    await report(0.05, "Fetching the model", now=True)
    src = await broker.get(f"/files/{file_id(mesh)}", timeout=300)
    src.raise_for_status()
    await report(0.25, "Blender: setting the height, checking the pose, exporting", now=True)
    work = tempfile.mkdtemp(prefix="snapfire-tool-")
    try:
        with open(os.path.join(work, "in.glb"), "wb") as f:
            f.write(src.content)
        out = os.path.join(work, "out")
        proc = await asyncio.create_subprocess_exec(
            BLENDER, "-b", "--factory-startup", "--python-exit-code", "1",
            "-P", os.path.join(SCRIPTS, "rig_ready.py"), "--", os.path.join(work, "in.glb"), out,
            str(height), pose, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            log_out, _ = await asyncio.wait_for(proc.communicate(), TOOL_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("blender took too long")
        if proc.returncode != 0:
            raise RuntimeError("blender failed: " + log_out.decode(errors="replace")[-400:])
        with open(os.path.join(out, "report.json")) as f:
            checks = json.load(f)  # not "report": that's the progress reporter, called just below
        files = {}
        await report(0.8, "Sending back the files", now=True)
        for name in ("front.png", "side.png", "hand.png", "character.glb", "character.fbx"):
            with open(os.path.join(out, name), "rb") as f:
                files[name] = await hand_in(broker, task, name, f.read())
    finally:
        shutil.rmtree(work, ignore_errors=True)
    shots = [files[n] for n in ("front.png", "side.png", "hand.png")]
    return {"output": {"image": shots[0]["file"], "images": shots, "mesh": files["character.glb"]["file"],
                       "fbx": files["character.fbx"]["file"], "files": [files["character.glb"], files["character.fbx"]],
                       "report": checks}, "model": task["model"]}


# ---- the loop ---------------------------------------------------------------

def measured_watts() -> float | None:
    """The GPU's power limit (GPU_WATTS, or nvidia-smi), or None when there's no way to read it."""
    if os.environ.get("GPU_WATTS"):
        return float(os.environ["GPU_WATTS"])
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=power.limit", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout.split()
        return float(out[0])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def gpu_watts() -> float:
    """The GPU's power limit, for working out what a unit of work costs in electricity."""
    return measured_watts() or 60.0  # a Mac, or no nvidia-smi


def gpu_name(comfy_device: str | None = None) -> str | None:
    """The card's name, so the broker can start this node at a floor that fits it: GPU_NAME, else what
    ComfyUI reports ("cuda:0 NVIDIA GeForce RTX 4090 : cudaMallocAsync"), else nvidia-smi."""
    if os.environ.get("GPU_NAME", "").strip():
        return os.environ["GPU_NAME"].strip()[:80]
    if comfy_device and comfy_device.startswith("cuda"):
        return comfy_device.split(" : ")[0].split(" ", 1)[-1][:80]
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=10).stdout.strip().splitlines()
        return out[0][:80] if out else None
    except (OSError, subprocess.SubprocessError):
        return None


class Floors:
    """The lowest price this node takes image work at, per model: what the electricity for one unit
    costs (watts x seconds per unit, at KWH_PRICE), plus a margin. Learned from the jobs it runs,
    so nobody has to guess a price. An owner who sets COMFY_PRICE gets that instead."""

    def __init__(self):
        self.watts = gpu_watts()
        self.sec_per_unit: dict[str, float] = {}
        self.learned: dict[str, int] = {}

    def floor(self, model: str) -> int:
        return self.learned.get(model, COMFY_PRICE)

    def learn(self, model: str, units: float, seconds: float) -> bool:
        """Record a finished run; True if the floor moved enough to tell the broker."""
        if COMFY_PRICE_SET or units <= 0 or seconds <= 0 or model not in MODELS:
            return False
        s = seconds / units
        old = self.sec_per_unit.get(model)
        self.sec_per_unit[model] = s if old is None else 0.7 * old + 0.3 * s
        dollars = self.watts * self.sec_per_unit[model] / 3_600_000 * KWH_PRICE  # W*s -> kWh -> $
        floor = max(1, math.ceil(dollars * 1_000_000 * FLOOR_MARGIN))
        before = MODELS[model]
        if abs(floor - before) <= max(1, before * 0.2):
            return False
        MODELS[model] = self.learned[model] = floor
        log.info("floor for %s is now %d micro-dollars per unit (%.2f s per unit at %d W)",
                 model, floor, self.sec_per_unit[model], self.watts)
        return True


FLOORS: Floors | None = None


async def worker(broker: httpx.AsyncClient, ollama: httpx.AsyncClient | None, comfy: httpx.AsyncClient | None,
                 n: int) -> None:
    while True:
        try:
            if comfy:
                await wait_for_gpu(comfy, broker)
            r = await broker.get("/provider/tasks/next", params={"wait": 20})
            if r.status_code == 409:  # broker restarted and forgot us
                await register(broker)
                continue
            r.raise_for_status()
            task = r.json()["task"]
            if not task:
                continue
            kind = task.get("kind", "llm")
            log.info("[%d] %s task %s", n, kind, task["id"])
            try:
                if kind == "comfyui":
                    if not comfy:
                        raise RuntimeError("this provider has no ComfyUI")
                    started = time.monotonic()
                    result = await run_comfy(comfy, broker, task)
                    if FLOORS and "error" not in result and FLOORS.learn(task["model"], float(task.get("units") or 0),
                                                                          time.monotonic() - started):
                        await register(broker, quiet=True)
                elif kind == "tool":
                    result = await run_tool(broker, task)
                else:
                    result = await run_ollama(ollama, task)
            except Stopped:
                log.info("task %s stopped: the customer stopped the job", task["id"])
                result = {"error": "stopped", "model": task.get("model") or MODEL}
            except Exception as e:  # report instead of going silent, so the broker can retry elsewhere
                log.warning("task %s failed: %s", task["id"], e)
                result = {"error": str(e) or type(e).__name__, "model": task.get("model") or MODEL}
            await broker.post(f"/provider/tasks/{task['id']}/result", json=result)
        except httpx.HTTPError as e:
            log.warning("broker unreachable (%s); retrying", e)
            await asyncio.sleep(3)


async def register(broker: httpx.AsyncClient, quiet: bool = False) -> None:
    paused = [m for m in MODELS if m.startswith("comfyui")] if GPU_STATE["paused"] else []
    free = shutil.disk_usage(COMFY_MODELS_DIR).free // 2 ** 20 if os.path.isdir(COMFY_MODELS_DIR) else None
    info = {"presets": sorted(PRESETS), "fetches_models": FETCH_MODELS, "disk_free_mb": free, "boot": BOOT,
            # for the broker's starting floor on this card, until the node has timed its own jobs
            "gpu": NODE["gpu"], "watts": measured_watts(), "kwh_price": KWH_PRICE, "price_set": bool(COMFY_PRICE_SET),
            "learned": sorted(FLOORS.learned) if FLOORS else []}
    files = sorted(NODE["files"]) if NODE["files"] is not None else None
    r = await broker.post("/provider/register", json={"models": MODELS, "paused": paused, "files": files,
                                                      "vram_mb": NODE["vram_mb"], "info": info})
    r.raise_for_status()
    if quiet:
        return
    log.info("registered %s serving %s", NAME, r.json()["models"])
    if r.json().get("suspended"):
        log.warning("this node is suspended after failing spot checks: it stays connected but gets no work")


async def wait_for(what: str, url: str, ready) -> None:
    for _ in range(120):
        try:
            await ready()
            return
        except (httpx.HTTPError, KeyError, ValueError):
            log.info("waiting for %s at %s", what, url)
            await asyncio.sleep(2)
    raise SystemExit(f"{what} at {url} never became ready")


async def main() -> None:
    global BLENDER
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ollama = httpx.AsyncClient(base_url=OLLAMA, timeout=600) if LLM_MODELS else None
    comfy = httpx.AsyncClient(base_url=COMFY, timeout=1800) if COMFY else None
    if not ollama and not comfy and not BLENDER:
        raise SystemExit("nothing to serve: set OLLAMA_URL/MODELS, COMFY_URL or BLENDER")
    if ollama:
        await wait_for("ollama", OLLAMA, lambda: ensure_models(ollama))
    if comfy:
        await wait_for("ComfyUI", COMFY, lambda: comfy.get("/system_stats"))

    key = os.environ.get("PROVIDER_KEY")
    async with httpx.AsyncClient(base_url=BROKER, timeout=60) as anon:
        while not key:
            try:
                r = await anon.post("/accounts", json={"kind": "provider", "name": NAME})
                r.raise_for_status()
                key = r.json()["api_key"]
            except httpx.HTTPError:
                log.info("waiting for broker at %s", BROKER)
                await asyncio.sleep(2)

    async with httpx.AsyncClient(base_url=BROKER, timeout=120,
                                 headers={"Authorization": f"Bearer {key}"}) as broker:
        if BLENDER:
            BLENDER = await prepare.blender(broker, BLENDER, BLENDER_HOME)
            if BLENDER and not os.access(BLENDER, os.X_OK):
                raise SystemExit(f"BLENDER={BLENDER} is not a program")
            if BLENDER:
                for name in NODE_TOOLS:
                    MODELS[f"tool:{name}"] = TOOL_PRICE
        if comfy:
            global FLOORS
            FLOORS = Floors()
            await load_comfy(comfy, broker)
        await register(broker)
        background = [asyncio.create_task(keep_fresh(comfy, broker))] if comfy else []
        if comfy and FETCH_MODELS and PRESETS:
            background.append(asyncio.create_task(fetch_models(comfy, broker)))
        await asyncio.gather(*(worker(broker, ollama, comfy, i) for i in range(CONCURRENCY)), *background)


async def load_comfy(comfy: httpx.AsyncClient, broker: httpx.AsyncClient) -> None:
    """Decide what to offer: checkpoints/diffusion models (limited to COMFY_MODELS, or to what
    the chosen presets use), and which files to report."""
    files, vram = await prepare.inventory(comfy)
    try:
        NODE["gpu"] = gpu_name((await comfy.get("/system_stats")).json()["devices"][0].get("name"))
    except (httpx.HTTPError, KeyError, IndexError, ValueError):
        NODE["gpu"] = gpu_name()
    allowed, preset_files = set(COMFY_ONLY), set()
    if PRESETS:
        try:
            r = await broker.get("/presets")
            r.raise_for_status()
            ps = prepare.wanted(r.json()["presets"], PRESETS)
        except (httpx.HTTPError, KeyError, ValueError) as e:
            # the broker is restarting or unreachable: keep offering what we offered, rather than nothing
            if any(k.startswith("comfyui:") for k in MODELS):
                log.warning("couldn't read the presets (%s): keeping the models already offered", e)
                return
            ps = []
        preset_files = {f for p in ps for f in p["needs"]["files"]}
        if not COMFY_ONLY:
            allowed = {m.removeprefix("comfyui:") for p in ps for m in p["needs"]["models"] if m.startswith("comfyui:")}
    for k in [k for k in MODELS if k.startswith("comfyui")]:
        del MODELS[k]
    for ckpt in await comfy_checkpoints(comfy):
        if not (COMFY_ONLY or PRESETS) or ckpt in allowed:
            MODELS[f"comfyui:{ckpt}"] = FLOORS.floor(f"comfyui:{ckpt}") if FLOORS else COMFY_PRICE
    MODELS["comfyui"] = FLOORS.floor("comfyui") if FLOORS else COMFY_PRICE  # workflows that load no checkpoint
    NODE["files"] = files if REPORT_FILES == "all" else files & (preset_files | allowed)
    NODE["vram_mb"] = vram


async def keep_fresh(comfy: httpx.AsyncClient, broker: httpx.AsyncClient) -> None:
    """Models added by hand show up within 10 minutes, without a restart."""
    while True:
        await asyncio.sleep(600)
        try:
            await load_comfy(comfy, broker)
            await register(broker, quiet=True)
        except httpx.HTTPError as e:
            log.warning("refresh failed: %s", e)


async def fetch_models(comfy: httpx.AsyncClient, broker: httpx.AsyncClient) -> None:
    if not COMFY_MODELS_DIR:
        log.warning("FETCH_MODELS=1 but COMFY_MODELS_DIR is not set: nothing fetched")
        return
    files, _ = await prepare.inventory(comfy)
    fetched = await prepare.fetch(broker, files, PRESETS, COMFY_MODELS_DIR, DISK_BUDGET_GB)
    if fetched:
        await asyncio.sleep(2)  # ComfyUI rescans its folders on the next listing
        await load_comfy(comfy, broker)
        await register(broker)


if __name__ == "__main__":
    asyncio.run(main())
