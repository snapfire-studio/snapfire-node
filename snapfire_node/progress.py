"""Real progress for a running ComfyUI task.

ComfyUI says what it is doing on its own WebSocket: which node is executing, and
for samplers which step of how many. Tracker turns that into one fraction and a
line of words. Reporter pushes them to the broker, at most once a second, and
learns from the reply when the customer has stopped the job.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx

try:
    import websockets
except ImportError:  # the node still works, it just can't say how far along it is
    websockets = None

log = logging.getLogger("snapfire.node")

# What a node type is doing, in words a customer reads.
LABELS = {
    "UNETLoader": "Loading the model", "CheckpointLoaderSimple": "Loading the model", "CLIPLoader": "Loading the model",
    "DualCLIPLoader": "Loading the model", "VAELoader": "Loading the model", "CLIPVisionLoader": "Loading the model",
    "LoadBackgroundRemovalModel": "Loading the model", "LoraLoader": "Loading the model",
    "LoadImage": "Reading the picture", "CLIPTextEncode": "Reading the prompt",
    "TextEncodeQwenImageEditPlus": "Reading the prompt and picture", "Trellis2Conditioning": "Looking at the picture",
    "RemoveBackground": "Removing the background", "ImageCropToMask": "Cropping to the subject",
    "VAEDecode": "Decoding the image", "VAEEncode": "Encoding the picture",
    "VaeDecodeStructureTrellis2": "Building the rough shape", "VoxelToMesh": "Building the rough shape",
    "VaeDecodeShapeTrellis": "Decoding the detailed shape", "VaeDecodeTextureTrellis": "Decoding the colors",
    "RemeshMesh": "Cleaning the mesh", "DecimateMesh": "Simplifying the mesh", "UnwrapMesh": "Unwrapping the surface",
    "BakeTextureFromVoxel": "Baking the textures", "BakeNormalMapFromMesh": "Baking surface detail",
    "BakeAmbientOcclusion": "Baking shadows", "ApplyTextureToMesh": "Applying the textures",
    "MeshSmoothNormals": "Smoothing", "SaveGLB": "Saving the model", "SaveImage": "Saving the image",
    "LoraLoaderModelOnly": "Loading the model", "UpscaleModelLoader": "Loading the model",
    "ImageScale": "Fitting the picture", "WanImageToVideo": "Preparing the frames",
    "CreateVideo": "Putting the frames together", "SaveVideo": "Encoding the video",
    "ImageUpscaleWithModel": "Upscaling", "LatentUpscaleBy": "Enlarging", "LatentUpscale": "Enlarging",
}
# Rough cost of a node next to one sampler step, so the bar moves at an even pace.
HEAVY = {
    "UNETLoader": 6, "CheckpointLoaderSimple": 6, "CLIPLoader": 4, "TextEncodeQwenImageEditPlus": 3,
    "RemoveBackground": 2, "VaeDecodeShapeTrellis": 5, "VaeDecodeTextureTrellis": 4, "RemeshMesh": 10,
    "DecimateMesh": 4, "UnwrapMesh": 8, "BakeTextureFromVoxel": 8, "BakeNormalMapFromMesh": 5,
    "BakeAmbientOcclusion": 8, "VAEDecode": 2, "SaveGLB": 2, "ImageUpscaleWithModel": 6, "SaveVideo": 2,
}


class Tracker:
    def __init__(self, wf: dict):
        self.wf = wf
        self.weight = {}
        for nid, node in wf.items():
            steps = (node.get("inputs") or {}).get("steps")
            self.weight[str(nid)] = float(steps) if isinstance(steps, (int, float)) and not isinstance(steps, bool) \
                else float(HEAVY.get(node.get("class_type"), 1))
        self.samplers = [nid for nid in self.weight if self._is_sampler(nid)]
        self.total = sum(self.weight.values()) or 1.0
        self.done: set[str] = set()
        self.current: str | None = None
        self.value = self.max = 0.0
        self.passes: list[str] = []  # samplers in the order they ran
        self.started = False

    def _is_sampler(self, nid: str) -> bool:
        steps = (self.wf[nid].get("inputs") or {}).get("steps") if nid in self.wf else None
        return isinstance(steps, (int, float)) and not isinstance(steps, bool)

    def on(self, kind: str, data: dict) -> None:
        if kind == "execution_cached":
            self.done |= {str(n) for n in data.get("nodes") or []}
        elif kind == "executing":
            if self.current:
                self.done.add(self.current)
            node = data.get("node")
            self.current = str(node) if node is not None else None
            self.value = self.max = 0.0
            self.started = True
            if self.current in self.samplers and self.current not in self.passes:
                self.passes.append(self.current)
        elif kind == "progress":
            node = data.get("node")
            if node is not None and str(node) != self.current:
                self.on("executing", {"node": node})
            self.value, self.max = float(data.get("value") or 0), float(data.get("max") or 0)

    @property
    def percent(self) -> float:
        got = sum(self.weight.get(n, 0) for n in self.done)
        if self.current and self.current not in self.done and self.max:
            got += self.weight.get(self.current, 0) * min(1.0, self.value / self.max)
        return min(1.0, got / self.total)

    @property
    def label(self) -> str:
        if not self.started:
            return "Starting"
        if not self.current or self.current not in self.wf:
            return "Finishing"
        if self.current in self.samplers:
            n, k = len(self.samplers), self.passes.index(self.current) + 1 if self.current in self.passes else 1
            step = f"step {int(self.value)} of {int(self.max)}" if self.max else "starting"
            return f"Sampling, pass {k} of {n}: {step}" if n > 1 else f"Sampling: {step}"
        return LABELS.get(self.wf[self.current].get("class_type"), "Working")


async def listen(comfy_url: str, client_id: str, tracker: Tracker, prompt: dict, connected: asyncio.Event) -> None:
    """Feed ComfyUI's messages for our prompt into the tracker until cancelled."""
    if websockets is None:
        connected.set()
        return
    url = comfy_url.replace("http://", "ws://").replace("https://", "wss://").rstrip("/") + f"/ws?clientId={client_id}"
    try:
        async with websockets.connect(url, max_size=None, open_timeout=5) as ws:
            connected.set()
            async for raw in ws:
                if isinstance(raw, bytes):
                    continue  # preview images
                msg = json.loads(raw)
                data = msg.get("data") or {}
                pid = data.get("prompt_id")
                if pid is not None and prompt.get("id") is not None and pid != prompt["id"]:
                    continue  # someone else's run on a shared ComfyUI
                tracker.on(msg.get("type", ""), data)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # progress is a nicety: never fail the task over it
        log.info("no live progress from ComfyUI (%s)", e)
    finally:
        connected.set()


class Stopped(Exception):
    """The broker said the customer stopped the job."""


class Reporter:
    """Tells the broker how far a task is. Calls are cheap: it sends at most once a second,
    and only when something changed."""

    def __init__(self, broker: httpx.AsyncClient, task_id: str):
        self.broker, self.task_id = broker, task_id
        self.last: tuple | None = None
        self.sent = 0.0
        self.off = False

    async def __call__(self, percent: float, label: str, *, now: bool = False) -> None:
        snap = (round(percent, 2), label)
        t = time.monotonic()
        if self.off or (snap == self.last and t - self.sent < 20) or (not now and t - self.sent < 1.0):
            return
        self.last, self.sent = snap, t
        try:
            r = await self.broker.post(f"/provider/tasks/{self.task_id}/progress",
                                       json={"percent": percent, "label": label}, timeout=10)
        except httpx.HTTPError:
            return
        if r.status_code == 404:
            self.off = True  # an older broker without progress
        elif r.status_code == 410 or (r.status_code == 200 and r.json().get("stop")):
            raise Stopped()
