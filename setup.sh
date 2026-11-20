#!/usr/bin/env bash
# Snapfire node setup. Asks a few questions, writes .env, starts the node in Docker
# and waits until the broker lists it.
#
# Run it again any time to change answers; your provider key is kept.
# To skip the questions, set them in the environment and pass ASSUME_YES=1:
#   SNAPFIRE_BROKER   broker address                      (asked, or read from broker.url)
#   NODE_NAME     name shown in the market            (this machine's hostname)
#   SERVE         images | text | both
#   COMFY_MODE    host | docker    where ComfyUI runs (host = already running on this machine)
#   OLLAMA_MODE   host | docker    where Ollama runs
#   COMFY_PRICE   micro-dollars per megapixel-step    (40)
#   TEXT_PRICE    micro-dollars per 1k tokens         (200)
#   PRESETS       presets to serve, comma separated, or all  (the broker lists them)
#   FETCH_MODELS  yes | no         let the node download the models those presets need
#   DISK_BUDGET_GB  most disk for downloaded models (100)
#   MODELS_DIR    ComfyUI's models folder (./models when ComfyUI runs in Docker)
#   JOIN_CODE     one-time code from the broker's "Run a node" page: the node's earnings go
#                 to your signed-in account. Without one the node gets an account of its own.
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
[ -z "${SNAPFIRE_BROKER:-}" ] && [ -f broker.url ] && SNAPFIRE_BROKER="$(cat broker.url)"
ask SNAPFIRE_BROKER "Broker address" "http://localhost:8400"
SNAPFIRE_BROKER="${SNAPFIRE_BROKER%/}"
HEALTH="$(curl -fsS -m 10 "$SNAPFIRE_BROKER/health" 2> /dev/null)" || die "Can't reach a Snapfire broker at $SNAPFIRE_BROKER."
POOL_MODEL="$(printf '%s' "$HEALTH" | json pool_model)"
say "Broker: $SNAPFIRE_BROKER (pool $(printf '%s' "$HEALTH" | json pool), text model $POOL_MODEL)"

ask NODE_NAME "Name for this node, shown in the market" "$(hostname -s 2> /dev/null || echo node)"
NODE_NAME="$(printf '%s' "$NODE_NAME" | tr -cd 'A-Za-z0-9._-' | cut -c1-40)"
[ -n "$NODE_NAME" ] || die "The node needs a name."

# ---- 3. what to serve -------------------------------------------------------
bold "What should this node serve?"
say "  images  ComfyUI workflows, paid per megapixel-step"
say "  text    LLM requests on $POOL_MODEL, paid per 1k tokens"
DEFAULT_SERVE=text
if [ "$HOST_COMFY" = yes ] || [ "$GPU" = nvidia ]; then DEFAULT_SERVE=images; fi
if { [ "$HOST_COMFY" = yes ] && [ "$HOST_OLLAMA" = yes ]; } || [ "$GPU" = nvidia ]; then DEFAULT_SERVE=both; fi
ask SERVE "Serve images, text or both" "$DEFAULT_SERVE"
case "$SERVE" in images|text|both) ;; *) die "Answer images, text or both." ;; esac

PROFILES=""; COMFY_URL=""; OLLAMA_URL=""; COMFY_ARGS=""; TORCH_INDEX=""
if [ "$SERVE" != text ]; then
  if [ "$HOST_COMFY" = yes ]; then DEF=host; else DEF=docker; fi
  if [ "$DEF" = docker ] && [ "$GPU" != nvidia ]; then
    say "ComfyUI in Docker would run on the CPU here: a minute or more per image. It works, but earns little."
  fi
  ask COMFY_MODE "Run ComfyUI on this machine itself (host) or in Docker (docker)" "$DEF"
  if [ "$COMFY_MODE" = host ]; then
    COMFY_URL="http://host.docker.internal:8188"
  else
    COMFY_URL="http://comfyui:8188"; PROFILES="comfyui"
    if [ "$GPU" != nvidia ]; then COMFY_ARGS="--cpu"; TORCH_INDEX="https://download.pytorch.org/whl/cpu"; fi
  fi
  ask COMFY_PRICE "Your price for image work, in micro-dollars per megapixel-step" "40"
fi
if [ "$SERVE" != images ]; then
  if [ "$HOST_OLLAMA" = yes ]; then DEF=host; else DEF=docker; fi
  ask OLLAMA_MODE "Run Ollama on this machine itself (host) or in Docker (docker)" "$DEF"
  if [ "$OLLAMA_MODE" = host ]; then
    OLLAMA_URL="http://host.docker.internal:11434"
  else
    OLLAMA_URL="http://ollama:11434"; PROFILES="${PROFILES:+$PROFILES,}ollama"
  fi
  ask TEXT_PRICE "Your price for text work, in micro-dollars per 1k tokens" "200"
  say "Ollama will download $POOL_MODEL the first time, if it doesn't have it yet."
fi

# ---- 4. presets and their models -------------------------------------------
MODELS_DIR="${MODELS_DIR:-}"
PRESETS="${PRESETS:-}"; FETCH="no"; BLENDER_SETTING=""
if [ "$SERVE" != text ]; then
  bold "Presets"
  say "Presets are tested workflows customers pick from a form. Choose the ones this node serves;"
  say "it can download their models itself (checked against the broker's checksums)."
  say ""
  curl -fsS -m 10 "$SNAPFIRE_BROKER/presets.txt" 2> /dev/null | sed 's/^/  /' || say "  (couldn't list them)"
  say ""
  DEF_PRESETS=quick-image
  [ "$GPU" = nvidia ] && DEF_PRESETS=all
  ask PRESETS "Presets to serve, comma separated, or all" "$DEF_PRESETS"
  if [ "${COMFY_MODE:-}" = host ]; then
    DEF_DIR=""
    for d in "$HOME/ComfyUI/models" "$HOME/comfyui/models" "$HOME/Documents/ComfyUI/models"; do
      [ -d "$d" ] && { DEF_DIR="$d"; break; }
    done
    ask MODELS_DIR "Your ComfyUI's models folder (empty: I'll add models myself)" "$DEF_DIR"
  else
    MODELS_DIR="${MODELS_DIR:-./models}"
  fi
  if [ -n "$MODELS_DIR" ]; then
    mkdir -p "$MODELS_DIR"
    ask FETCH_MODELS "Download the models these presets need (now and when they change)? yes or no" "yes"
    case "$FETCH_MODELS" in y|yes) FETCH=yes ;; esac
    [ "$FETCH" = yes ] && ask DISK_BUDGET_GB "Most disk to use for downloaded models, in GB" "100"
  fi
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
COMFY_PRICE=${COMFY_PRICE:-40}
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
printf 'Waiting for %s to join the pool' "$NODE_NAME"
ONLINE=no
for _ in $(seq 1 180); do
  if docker compose logs worker 2> /dev/null | grep -q "registered $NODE_NAME "; then ONLINE=yes; break; fi
  printf '.'; sleep 5
done
echo

if [ "$ONLINE" = yes ]; then
  bold "$NODE_NAME is online and taking work."
else
  bold "$NODE_NAME has not joined yet."
  say "It may still be building. Check with:  docker compose logs -f worker"
fi
say ""
[ "$FETCH" = yes ] && say "Models for $PRESETS download in the background; the log shows progress."
say "  Watch it work      docker compose logs -f worker"
say "  See your earnings  curl -s -H \"Authorization: Bearer \$(grep ^PROVIDER_KEY= .env | cut -d= -f2)\" $SNAPFIRE_BROKER/accounts/me"
say "  Change prices      edit .env, then: docker compose up -d"
say "  Stop the node      docker compose down"
say "  This folder        $(pwd)"
[ "$ONLINE" = yes ] || exit 1
