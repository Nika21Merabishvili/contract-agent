# Runs app.py against the Modal-hosted Ollama GPU instance instead of local
# Ollama. OLLAMA_HOST only applies to this PowerShell process, so plain
# `python app.py` in any other window still defaults to local Ollama
# (127.0.0.1:11434) -- this script is the only thing that points at Modal.
#
# Usage:
#   .\run_modal.ps1

$env:OLLAMA_HOST = "https://nikamerabishvili21--nxia-ollama-serve.modal.run"
Write-Host "OLLAMA_HOST set to $env:OLLAMA_HOST for this session"
python app.py
