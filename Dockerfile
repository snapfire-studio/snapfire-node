# The Snapfire worker: pulls tasks from the broker and hands them to Ollama / ComfyUI.
FROM python:3.12-slim
# Libraries the portable Blender needs to run headless (for the 3D presets; BLENDER=auto fetches Blender)
RUN apt-get update && apt-get install -y --no-install-recommends libx11-6 libxrender1 libxxf86vm1 libxfixes3 \
      libxi6 libxkbcommon0 libsm6 libgl1 libegl1 libgomp1 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "httpx>=0.27" "websockets>=12"
WORKDIR /app
COPY snapfire_node ./snapfire_node
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "snapfire_node"]
