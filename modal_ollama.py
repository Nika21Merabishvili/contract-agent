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

MODEL = "qwen3.6:35b"
OLLAMA_PORT = 11434

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("curl", "zstd")
    .run_commands("curl -fsSL https://ollama.com/install.sh | sh")
    # Bake the model weights into the image at build time so a cold start
    # doesn't have to pull multiple GB from Ollama's registry.
    .run_commands(
        "ollama serve & "
        "SERVER_PID=$!; "
        "sleep 5 && ollama pull " + MODEL + "; "
        "kill $SERVER_PID"
    )
    .env({"OLLAMA_HOST": f"0.0.0.0:{OLLAMA_PORT}"})
)

app = modal.App("nxia-ollama", image=image)


@app.function(
    gpu="A10G",              # 24GB: qwen3.6:35b weights (~23GB) leave little headroom for KV cache -- watch for OOM
    scaledown_window=300,    # keep the container warm 5 min after the last request, then scale to zero
    timeout=600,
)
@modal.web_server(port=OLLAMA_PORT, startup_timeout=60)
def serve():
    subprocess.Popen(["ollama", "serve"])
    # Give the server a moment to bind before Modal starts health-checking it.
    time.sleep(2)
