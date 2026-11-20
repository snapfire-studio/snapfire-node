# ComfyUI server for a Snapfire node. Stock nodes only: nothing is added to custom_nodes.
# With the default PyTorch wheel and the NVIDIA Container Toolkit it uses the GPU.
# Without a GPU it runs on the CPU: build with the CPU wheel and start it with --cpu.
FROM python:3.12-slim
ARG COMFYUI_REF=v0.45.0
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch ${COMFYUI_REF} https://github.com/comfyanonymous/ComfyUI.git /comfyui
WORKDIR /comfyui
# TORCH_INDEX picks the PyTorch build. Empty: the default wheel, which carries CUDA
# (about 10 GB). setup.sh passes the CPU index when there is no NVIDIA GPU.
ARG TORCH_INDEX=""
RUN if [ -n "$TORCH_INDEX" ]; then pip install --no-cache-dir torch torchvision --index-url "$TORCH_INDEX"; fi \
 && pip install --no-cache-dir -r requirements.txt
ENV PYTHONUNBUFFERED=1
EXPOSE 8188
CMD ["python", "main.py", "--listen", "0.0.0.0", "--port", "8188"]
