# Run a Snapfire node

A node takes tasks from a Snapfire broker, runs them on your hardware and gets paid
per task. One command sets it up:

```bash
curl -fsSL https://snapfire.studio/install.sh | bash
```

It downloads this folder to `~/snapfire-node` and runs `setup.sh`, which asks what
to serve, writes `.env`, starts the node in Docker and waits until the broker
lists it. Run `./setup.sh` again any time to change your answers.

To tie the node to your account, sign in on the broker's **Run a node** page
and copy the command it makes there. It carries a one-time `JOIN_CODE`, so the
node's earnings show under **Account** and can be moved onto your credit to
spend on jobs. Without one, the node gets an account of its own.

## What you need

- Docker with Compose v2.
- For image work (ComfyUI), one of:
  - Linux with an NVIDIA GPU and the NVIDIA Container Toolkit. ComfyUI runs in Docker on the GPU.
  - A Mac with ComfyUI running on the Mac itself (port 8188). Docker on a Mac
    can't use the GPU, so the node's worker runs in Docker and uses your ComfyUI.
  - Anything else works on the CPU, at a minute or more per image.
- For text work: Ollama, either already running on the machine (port 11434) or in Docker.

## What it runs

| Container | When | What it does |
|---|---|---|
| `worker` | always | Dials out to the broker, pulls a task, hands it to ComfyUI or Ollama, returns the result. No ports are opened. |
| `comfyui` | image work in Docker | A stock ComfyUI server. Checkpoints come from `./models/checkpoints`. |
| `ollama` | text work in Docker | Pulls the pool's text model on first start. |

## What you are agreeing to run

- Other people's prompts and ComfyUI workflows. You can see them, and the
  images you produce.
- Only stock ComfyUI nodes: the broker rejects workflows with custom nodes, and
  the ComfyUI image has none installed.
- Image work only arrives for checkpoints you have. Put more `.safetensors`
  files in `./models/checkpoints` and restart to serve more.

## Getting paid

You set the price in `.env`: `COMFY_PRICE` per megapixel-step (sampler steps x
megapixels x batch size) and `PRICE_PER_1K` per 1,000 tokens. Tasks go to the
cheapest free node that can run them, and each node is paid its own price,
minus the broker's fee. Image tasks pay a price fixed before they run.

```bash
# earnings so far, in micro-dollars
curl -s -H "Authorization: Bearer $(grep ^PROVIDER_KEY= .env | cut -d= -f2)" https://snapfire.studio/accounts/me
```

`PROVIDER_KEY` in `.env` is the node's account. Keep the file private; setup
makes it readable only by you.

## Checks

Now and then a task your node finished is run again on another node and the
results compared. Images must match. Text nodes also get questions with one right
answer, like a small sum. If two nodes' images disagree a third decides, and the
node that loses has its pay for that task taken back. A node that fails often is suspended: it
stays connected but gets no work. New nodes are checked more often.

Run the real model you registered, unmodified, and checks pass on their own.

## Day to day

```bash
docker compose logs -f worker   # watch tasks arrive
docker compose up -d            # apply changes to .env
docker compose down             # stop
./setup.sh                      # change what you serve; your account is kept
```

## Presets and models

Set `PRESETS` to the presets this node serves (`all`, or a list like
`quick-image,remove-background`; the broker's `/presets.txt` lists them with
sizes). With `FETCH_MODELS=1` and `COMFY_MODELS_DIR` set to ComfyUI's
`models/` folder, the node downloads whatever those presets need and it lacks,
checks every file against the catalog's sha256, and stays within
`DISK_BUDGET_GB` (and 10 GB of free disk). It reports its model files and GPU
memory to the broker, so it only gets work it can run; `REPORT_FILES=all` also
reports files no preset uses, so uploaded workflows can find it. `BLENDER=auto`
finds Blender or fetches the portable Linux build for `character-3d`.

## Progress and stopping

The worker listens to ComfyUI's WebSocket and tells the broker how far each task is, about once a second, so
customers see "Sampling: step 12 of 20" instead of a spinner. It needs the `websockets` package (in the Docker
image; `pip install websockets` otherwise); without it the node still works, with no progress shown. When a
customer stops a job, the node breaks off that run in ComfyUI and is paid for the part it did.

## Serving 3D characters

The `character3d` request needs a node with an NVIDIA GPU (16 GB is enough), a
ComfyUI of 0.34 or newer, and Blender. Put these in ComfyUI's `models/`:

| Folder | File | From |
|---|---|---|
| `diffusion_models` | `trellis_2_int8_convrot.safetensors` (4.9 GB) | huggingface.co/Comfy-Org/TRELLIS.2 |
| `vae` | `trellis_2_shape_vae_bf16.safetensors`, `trellis_2_texture_vae_bf16.safetensors` | huggingface.co/Comfy-Org/Pixal3D |
| `clip_vision` | `dino_v3_L_naf_fp32.safetensors` | huggingface.co/Comfy-Org/Pixal3D |
| `background_removal` | `birefnet.safetensors` | huggingface.co/Comfy-Org/BiRefNet |
| `diffusion_models` | `qwen_image_edit_2511_fp8_e4m3fn_scaled_lightning_comfyui_4steps_v1.0.safetensors` | Comfy-Org/Qwen-Image-Edit_ComfyUI |
| `text_encoders`, `vae` | `qwen_2.5_vl_7b_fp8_scaled.safetensors`, `qwen_image_vae.safetensors` | Comfy-Org/Qwen-Image_ComfyUI |

Unpack a Blender Linux build (the portable `.tar.xz`) anywhere and point
`BLENDER` at its `blender` binary. Useful settings:

- `COMFY_MODELS=qwen_image_edit_…safetensors,trellis_2_int8_convrot.safetensors`
  offers only those files, not everything your ComfyUI has.
- `GPU_NEED_MB=11000` shares the card: while other programs leave less than
  that free, the node takes no work (it stays listed, so jobs still queue for
  it). If a run still runs out of memory, the node frees ComfyUI's models and
  tries again.

## Without Docker

The worker is one Python file with one dependency:

```bash
pip install httpx websockets
BROKER_URL=https://snapfire.studio PROVIDER_NAME=my-node COMFY_URL=http://127.0.0.1:8188 OLLAMA_URL= \
  BLENDER=/opt/blender/blender python -m snapfire_node
```
