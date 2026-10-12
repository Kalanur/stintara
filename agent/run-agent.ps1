$ErrorActionPreference = "Stop"
if (-not $env:PITWALL_WS) { $env:PITWALL_WS = "ws://127.0.0.1:9000?format=json" }
if (-not $env:STINTARA_WS) { throw "Set STINTARA_WS to ws://<unraid-host>:8080/ws/ingest" }
if (-not $env:STINTARA_AGENT_TOKEN) { throw "Set STINTARA_AGENT_TOKEN to the token configured on the server" }
python "$PSScriptRoot/stintara_agent.py"
