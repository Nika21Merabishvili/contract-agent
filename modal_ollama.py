"""Runs Ollama on a Modal GPU container and exposes it as an HTTP endpoint.

This is a drop-in replacement for the local `ollama serve` your pipeline talks
to -- same API (`/api/chat`, `/api/generate`, ...), just running on a rented
GPU instead of this machine's CPU. Nothing in ollama_client.py, pipeline.py,
or app.py changes except which host it points at (see OLLAMA_HOST below).

Deploy:
    modal deploy modal_ollama.py

Deploying prints a URL like:
    https://<workspace>--nxia-ollama-serve.modal.run
Point your pipeline at it:
    set OLLAMA_HOST=https://<workspace>--nxia-ollama-serve.modal.run   (Windows)
    export OLLAMA_HOST=https://<workspace>--nxia-ollama-serve.modal.run (bash)

The container scales to zero when idle (see `scaledown_window`), so you are
only billed for GPU seconds while a request is actually in flight or a warm
container is waiting out its idle window.
"""

import subprocess
import time

import modal

MODEL = "qwen3.6:35b"        # steps 1-4, the analysis pipeline (ollama_client.py)
OCR_MODEL = "glm-ocr:bf16"   # scanned-PDF OCR (ocr_glm.py)
MODELS = (MODEL, OCR_MODEL)
OLLAMA_PORT = 11434

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("curl", "zstd")
    .run_commands("curl -fsSL https://ollama.com/install.sh | sh")
    # Bake every model's weights into the image at build time so a cold start
    # doesn't have to pull multiple GB from Ollama's registry. Both the analysis
    # model and the OCR model are baked in, so a scanned contract is OCR'd on the
    # GPU too, not just the four analysis calls.
    .run_commands(
        "ollama serve & "
        "SERVER_PID=$!; "
        "sleep 5" + "".join(f" && ollama pull {m}" for m in MODELS) + "; "
        "kill $SERVER_PID"
    )
    .env({"OLLAMA_HOST": f"0.0.0.0:{OLLAMA_PORT}"})
)

app = modal.App("nxia-ollama", image=image)


@app.function(
    gpu="A10G",              # 24GB: qwen3.6:35b (~23GB) nearly fills it, so Ollama swaps it out to load glm-ocr for a scanned PDF and reloads it for analysis -- correct, but that reload is slow. A bigger GPU (e.g. A100 40GB) would hold both at once.
    scaledown_window=300,    # keep the container warm 5 min after the last request, then scale to zero
    timeout=600,
)
@modal.web_server(port=OLLAMA_PORT, startup_timeout=60)
def serve():
    subprocess.Popen(["ollama", "serve"])
    # Give the server a moment to bind before Modal starts health-checking it.
    time.sleep(2)
