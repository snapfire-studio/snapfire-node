#!/usr/bin/env bash
# Snapfire node setup. Looks at the machine, writes .env, starts the node in Docker and waits
# until Snapfire lists it. It asks nothing it can work out itself.
#
# Run it again any time; the node's account and name are kept.
# Everything is worked out from the machine. To override, set these in the environment:
#   SNAPFIRE_BROKER  Snapfire address              (read from broker.url, written by install.sh)
#   COMFY_MODE    host | docker   where ComfyUI runs (Mac: host, NVIDIA: docker)
#   PRESETS       presets to serve, comma separated, or all   (NVIDIA: all; Mac: the light ones)
#   MODELS_DIR    ComfyUI's models folder (./models when ComfyUI runs in Docker)
#   FETCH_MODELS  yes | no        let the node download the models those presets need (yes)
#   DISK_BUDGET_GB  most disk for downloaded models (100)
#   SERVE         images | text | both   (images)
#   JOIN_CODE     one-time code from the Run a node page: the node's earnings go to your account.
set -euo pipefail
cd "$(dirname "$0")"

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
say()  { printf '%s\n' "$*"; }
die()  { printf '\nError: %s\n' "$*" >&2; exit 1; }
YES="${ASSUME_YES:-0}"

ask() { # ask VAR "question" "default": keeps a value that is already set
  local var="$1" q="$2" def="${3:-}" cur ans=""
  eval "cur=\${$var:-}"
  if [ -n "$cur" ]; then return 0; fi
  if [ "$YES" != 1 ] && { : > /dev/tty; } 2> /dev/null; then
    printf '%s [%s]: ' "$q" "$def" > /dev/tty
    read -r ans < /dev/tty || ans=""
  fi
  eval "$var=\${ans:-\$def}"
}
up()        { curl -fsS -m 3 "$1" > /dev/null 2>&1; }
in_docker() { printf '%s' "$1" | sed -E 's#//(localhost|127\.0\.0\.1)#//host.docker.internal#'; }
json()      { sed -n "s/.*\"$1\":\"\\([^\"]*\\)\".*/\\1/p"; }   # one string field out of a small JSON reply

# ---- 1. what is on this machine ---------------------------------------------
bold "Snapfire node setup"
command -v docker > /dev/null   || die "Docker is not installed. Get it at https://docs.docker.com/get-docker/ and run this again."
docker info > /dev/null 2>&1    || die "Docker is installed but not running. Start it and run this again."
docker compose version > /dev/null 2>&1 || die "Docker Compose v2 is missing (the 'docker compose' command)."
command -v curl > /dev/null     || die "curl is needed."

OS="$(uname -s)"
GPU=none
if command -v nvidia-smi > /dev/null 2>&1 && nvidia-smi -L > /dev/null 2>&1; then
  if docker info 2> /dev/null | grep -qi nvidia; then GPU=nvidia; else GPU=nvidia-no-toolkit; fi
fi
HOST_COMFY=no; up http://localhost:8188/system_stats && HOST_COMFY=yes
HOST_OLLAMA=no; up http://localhost:11434/api/tags && HOST_OLLAMA=yes

case "$GPU" in
  nvidia) say "GPU: NVIDIA, usable from Docker." ;;
  nvidia-no-toolkit) say "GPU: NVIDIA found, but Docker can't use it yet. Install the NVIDIA Container Toolkit"
                     say "     (https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/) and run this again."
                     say "     Continuing without the GPU." ;;
  *) if [ "$OS" = Darwin ]; then say "GPU: Docker on a Mac can't use the Mac's GPU. Run ComfyUI or Ollama on the Mac itself and this node will use them."
     else say "GPU: none found. Docker services will run on the CPU."; fi ;;
esac
[ "$HOST_COMFY" = yes ]  && say "Found ComfyUI running on this machine (port 8188)."
[ "$HOST_OLLAMA" = yes ] && say "Found Ollama running on this machine (port 11434)."

# ---- 2. the broker ----------------------------------------------------------
[ -f .env ] && PROVIDER_KEY="${PROVIDER_KEY:-$(grep '^PROVIDER_KEY=' .env | cut -d= -f2- || true)}"
[ -f .env ] && NODE_NAME="${NODE_NAME:-$(grep '^PROVIDER_NAME=' .env | cut -d= -f2- || true)}"
[ -z "${SNAPFIRE_BROKER:-}" ] && [ -f broker.url ] && SNAPFIRE_BROKER="$(cat broker.url)"
ask SNAPFIRE_BROKER "Snapfire address" "https://snapfire.studio"
SNAPFIRE_BROKER="${SNAPFIRE_BROKER%/}"
HEALTH="$(curl -fsS -m 10 "$SNAPFIRE_BROKER/health" 2> /dev/null)" || die "Can't reach Snapfire at $SNAPFIRE_BROKER."
POOL_MODEL="$(printf '%s' "$HEALTH" | json pool_model)"

# The market shows a generated name, not one people pick (it would be shown to strangers).
if [ -z "${NODE_NAME:-}" ]; then
  NODE_NAME="node-$(od -An -N3 -tx1 /dev/urandom | tr -d ' \n')"
fi
NODE_NAME="$(printf '%s' "$NODE_NAME" | tr -cd 'A-Za-z0-9._-' | cut -c1-40)"

# ---- 3. how it runs: decided from the machine, no questions ------------------
# Images only: the presets are image, video and 3D work. (SERVE=text|both still works for the text pool.)
SERVE="${SERVE:-images}"
PROFILES=""; COMFY_URL=""; OLLAMA_URL=""; COMFY_ARGS=""; TORCH_INDEX=""
if [ "$SERVE" != text ]; then
  if [ -z "${COMFY_MODE:-}" ]; then
    if [ "$OS" = Darwin ]; then
      # Docker on a Mac can't reach the Mac's GPU, so ComfyUI has to run on the Mac itself.
      [ "$HOST_COMFY" = yes ] || die "On a Mac, ComfyUI runs outside Docker so it can use the GPU.
       Install ComfyUI Desktop from https://www.comfy.org/download, open it, then run this again."
      COMFY_MODE=host
    elif [ "$GPU" = nvidia ]; then
      COMFY_MODE=docker
    else
      die "This machine has no GPU that Docker can use. A node needs an NVIDIA GPU (Linux or Windows with WSL)
       with the NVIDIA Container Toolkit, or an Apple Silicon Mac running ComfyUI Desktop."
    fi
  fi
  if [ "$COMFY_MODE" = host ]; then
    COMFY_URL="http://host.docker.internal:8188"
  else
    COMFY_URL="http://comfyui:8188"; PROFILES="comfyui"
  fi
fi
if [ "$SERVE" != images ]; then
  if [ "$HOST_OLLAMA" = yes ]; then OLLAMA_MODE="${OLLAMA_MODE:-host}"; else OLLAMA_MODE="${OLLAMA_MODE:-docker}"; fi
  if [ "$OLLAMA_MODE" = host ]; then OLLAMA_URL="http://host.docker.internal:11434"
  else OLLAMA_URL="http://ollama:11434"; PROFILES="${PROFILES:+$PROFILES,}ollama"; fi
fi
# Prices are Snapfire's standard rates: nodes don't set their own.

# ---- 4. presets and their models -------------------------------------------
# A node serves every preset its GPU has room for (the worker checks each one's memory need)
# and downloads the models itself, checked against Snapfire's checksums.
MODELS_DIR="${MODELS_DIR:-}"
FETCH="no"; BLENDER_SETTING=""
if [ "$SERVE" != text ]; then
  if [ "$COMFY_MODE" = host ]; then
    for d in "$HOME/Documents/ComfyUI/models" "$HOME/ComfyUI/models" "$HOME/comfyui/models"; do
      [ -z "$MODELS_DIR" ] && [ -d "$d" ] && MODELS_DIR="$d"
    done
    ask MODELS_DIR "Couldn't find ComfyUI's models folder. Where is it?" ""
    # a Mac runs the lighter presets; the big video models need an NVIDIA card
    PRESETS="${PRESETS:-quick-image,remove-background,upscale}"
  else
    MODELS_DIR="${MODELS_DIR:-./models}"
    PRESETS="${PRESETS:-all}"
  fi
  [ -n "$MODELS_DIR" ] || die "ComfyUI's models folder is needed so the node can add the models it serves."
  mkdir -p "$MODELS_DIR"
  case "${FETCH_MODELS:-yes}" in y|yes|1) FETCH=yes ;; esac
  case ",$PRESETS," in *,all,*|*,character-3d,*) BLENDER_SETTING=auto ;; esac
fi

# ---- 5. a provider account --------------------------------------------------
if [ -z "${PROVIDER_KEY:-}" ] && [ -n "${JOIN_CODE:-}" ]; then
  REPLY_JSON="$(curl -fsS -m 10 -X POST "$SNAPFIRE_BROKER/accounts/join" -H 'Content-Type: application/json' \
    -d "{\"code\":\"$JOIN_CODE\",\"name\":\"$NODE_NAME\"}")" \
    || die "The broker didn't accept the join code. Codes work once and for an hour: make a new one on the Run a node page."
  PROVIDER_KEY="$(printf '%s' "$REPLY_JSON" | json api_key)"
  [ -n "$PROVIDER_KEY" ] || die "Got no key back from the broker."
  say "Joined your account as $NODE_NAME."
elif [ -z "${PROVIDER_KEY:-}" ]; then
  REPLY_JSON="$(curl -fsS -m 10 -X POST "$SNAPFIRE_BROKER/accounts" -H 'Content-Type: application/json' \
    -d "{\"kind\":\"provider\",\"name\":\"$NODE_NAME\"}")" || die "The broker refused to create a provider account."
  PROVIDER_KEY="$(printf '%s' "$REPLY_JSON" | json api_key)"
  [ -n "$PROVIDER_KEY" ] || die "Got no key back from the broker."
  say "Created a provider account for $NODE_NAME (not tied to anyone; sign in on the broker and use a join code to tie it to you)."
else
  say "Keeping the provider account from the last setup."
fi

# ---- 6. write .env and start ------------------------------------------------
COMPOSE_FILE="docker-compose.yml"
[ "$GPU" = nvidia ] && COMPOSE_FILE="docker-compose.yml:docker-compose.nvidia.yml"
umask 077
cat > .env <<EOF
# Written by setup.sh. To change something: edit, then run  docker compose up -d
# PROVIDER_KEY is this node's account: whoever has it can read its balance.
BROKER_URL=$(in_docker "$SNAPFIRE_BROKER")
PROVIDER_NAME=$NODE_NAME
PROVIDER_KEY=$PROVIDER_KEY
CONCURRENCY=1

# text work (empty OLLAMA_URL = none)
OLLAMA_URL=$OLLAMA_URL
TEXT_MODEL=$POOL_MODEL
PRICE_PER_1K=${TEXT_PRICE:-200}

# image work (empty COMFY_URL = none)
COMFY_URL=$COMFY_URL
# your floor for image work (empty: worked out from your GPU's electricity cost; you are paid the market price)
COMFY_PRICE=${COMFY_PRICE:-}
COMFY_ARGS=$COMFY_ARGS
TORCH_INDEX=$TORCH_INDEX
MODELS_DIR=${MODELS_DIR:-./models}

# presets: what this node serves, and whether it downloads their models itself
PRESETS=$PRESETS
FETCH_MODELS=$([ "$FETCH" = yes ] && echo 1 || echo 0)
DISK_BUDGET_GB=${DISK_BUDGET_GB:-100}
# "auto": find Blender, or fetch the portable Linux build (for character-3d)
BLENDER=$BLENDER_SETTING

# which Docker services run besides the worker
COMPOSE_PROFILES=$PROFILES
COMPOSE_FILE=$COMPOSE_FILE
EOF

bold "Starting the node"
say "Building. The first time this can take several minutes, longer if ComfyUI runs in Docker."
docker compose build --quiet
docker compose up -d --remove-orphans

# The worker logs "registered <name>" once the broker has accepted it. Reading its own log,
# not the market list, means a leftover entry from an earlier run can't pass for this one.
printf 'Waiting for the node to join'
ONLINE=no
for _ in $(seq 1 180); do
  if docker compose logs worker 2> /dev/null | grep -q "registered $NODE_NAME "; then ONLINE=yes; break; fi
  printf '.'; sleep 5
done
echo

if [ "$ONLINE" = yes ]; then
  bold "Your node is online and taking work."
else
  bold "Your node has not joined yet."
  say "It may still be building. Check with:  docker compose logs -f worker"
fi
say ""
[ "$FETCH" = yes ] && say "The models download in the background; the log shows progress."
say "  Watch it work      docker compose logs -f worker"
say "  See your earnings  curl -s -H \"Authorization: Bearer \$(grep ^PROVIDER_KEY= .env | cut -d= -f2)\" $SNAPFIRE_BROKER/accounts/me"
say "  Stop the node      docker compose down"
say "  This folder        $(pwd)"
[ "$ONLINE" = yes ] || exit 1
