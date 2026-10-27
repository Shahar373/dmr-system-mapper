# Working agreements for dmr-system-mapper

- **Language:** converse with the user in Hebrew. Code, comments, documentation and commit
  messages stay in English unless the user asks otherwise.
- **Phases:** work is delivered in phases (see README.md). Commit at the end of each phase with a
  clear message and push to `main`, then stop and wait for the user's approval before the next phase.
- **Reference projects are read-only:** dmr-iq-surveyor (MIT), DMR-DECREP-SHAHAR (no licence: re-implement,
  do not copy) and the private P25 mapper. Copy ideas and parser knowledge, never modify them.
- **No DSP of our own beyond channelisation:** DSD-FME does all DMR decoding. Claude's role through MCP is
  orchestration and judgement over evidence.
- **Never hardcode machine-specific values** (Tailscale addresses, usernames, paths, band plans). They go in
  `config.local.yaml` (git-ignored); `config.example.yaml` is the committed template.
- **Storage discipline:** the collector streams DSD-FME output and writes only structured, aggregated events to
  SQLite. Raw logs only under a debug flag with size-capped rotation. No IQ or WAV is ever kept.
- **samples/ are test fixtures.** Everything must be testable offline on them before hardware is involved.
  DSD-FME writes its console to stderr: the surveyor `*_stdout.log` files are empty by design.
- **Tier II links are never presented as certain.** Every link carries a confidence score and its evidence.
- Python 3.11+, SQLite, NetworkX; keep dependencies minimal.
