# Runs app.py against a Modal-hosted Ollama GPU instance instead of local
# Ollama. Point it at YOUR OWN Modal endpoint by setting MODAL_OLLAMA_HOST first
# -- deploy modal_ollama.py under your own Modal account and use the URL that
# `modal deploy` prints (see "Running on a Modal GPU" in README.md).
#
# OLLAMA_HOST only applies to this PowerShell process, so plain `python app.py`
# in any other window still defaults to local Ollama (127.0.0.1:11434) -- this
# script is the only thing that points at Modal.
#
# Usage:
#   $env:MODAL_OLLAMA_HOST = "https://<your-workspace>--nxia-ollama-serve.modal.run"
#   .\run_modal.ps1

if (-not $env:MODAL_OLLAMA_HOST) {
    Write-Host "error: MODAL_OLLAMA_HOST is not set." -ForegroundColor Red
    Write-Host "Deploy modal_ollama.py under your own Modal account, then set it to the"
    Write-Host "URL that 'modal deploy' printed, e.g.:"
    Write-Host '  $env:MODAL_OLLAMA_HOST = "https://<your-workspace>--nxia-ollama-serve.modal.run"'
    exit 1
}

$env:OLLAMA_HOST = $env:MODAL_OLLAMA_HOST
Write-Host "OLLAMA_HOST set to $env:OLLAMA_HOST for this session"
python app.py
