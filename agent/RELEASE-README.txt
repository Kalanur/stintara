Stintara Agent (Windows, unsigned test build)

Files
  StintaraAgent.exe             the agent
  stintara-agent.example.json   configuration template (cannot authenticate)

Setup
  1. Copy stintara-agent.example.json to stintara-agent.json next to the EXE.
  2. Fill in the server URL and the token supplied by your operator.
     Keep this file private; never share or publish it.
  3. Start LMU with the Pitwall bridge, then run StintaraAgent.exe.

Check the build (no configuration or network required)
  StintaraAgent.exe --version

Verify the download
  Compare the SHA256 of this ZIP with the published SHA256SUMS.txt.

Known limitation
  The executable is not code-signed. Windows SmartScreen or antivirus tools may
  warn about it. Only run builds obtained from the official GitHub release.

Compatibility
  Agent 0.2.0 and later require a Stintara server that includes the Track Map /
  Race Control support (PR #39 or newer). Deploy the server first.
