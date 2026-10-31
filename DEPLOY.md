# DEPLOY.md: running dmr-system-mapper on the Pi

The Pi runs one always-on service, `dmr-mapper`, which is idle until you start a session from
the console (`mapper tui`) or the shell. A nightly timer takes a consistent snapshot of the
database, and a second Pi (the archive) pulls those snapshots over Tailscale with a read-only key.

Nothing machine-specific is committed: `deploy/deploy.sh` fills in the user, group and paths of
the machine it runs on; `config.local.yaml` (git-ignored) holds local settings.

## 1. First install

The clone and its `.venv` already exist if you followed the Phase 3a steps; `deploy.sh` creates
them otherwise.

```sh
cd ~/Projects/dmr-system-mapper
git pull
deploy/deploy.sh
```

If a daemon you started by hand (`mapper daemon`) is still running, stop it first (Ctrl-C in its
window); deploy.sh refuses to start a second one. It asks for your sudo password to install three
unit files into `/etc/systemd/system/`:

| Unit | What it does |
|---|---|
| `dmr-mapper.service` | the daemon, started at boot, restarted if it crashes; holds no receiver while idle |
| `dmr-mapper-snapshot.timer` | every night at `archive.snapshot_time` (03:30), or at the next boot if the Pi was off |
| `dmr-mapper-snapshot.service` | `mapper prune` (90-day retention), correlate, `mapper snapshot` (to `var/snapshots/`), `mapper export --all` (to `var/exports/`) |

It also links `~/.local/bin/mapper`, so `mapper` works from any folder without activating the
venv (after the next login, if `~/.local/bin` was not on the PATH yet).

Recommended `config.local.yaml` next to `config.example.yaml`:

```yaml
power:
  state_file: /run/dmr/power.json    # the power guard's state file, if that service is installed
gps:
  device: /dev/ttyACM0               # only if `mapper doctor` does not find the GPS by itself
```

`mapper doctor` shows which GPS source a session would use. If another program reads the GPS
device directly, the mapper stays away from it and asks for gpsd, which shares one GPS safely.

## 2. Daily use

```sh
mapper tui                        # the console: a survey+decode, s survey, d decode, w watch, x stop, c correlate, e export, n name, y/v approve/refuse Claude, q quit
mapper status                     # one-shot summary
mapper watch --minutes 20         # Capacity Max: one slice, the control channel and every traffic channel that keys up
journalctl -u dmr-mapper -f       # what the daemon is doing (sessions, refusals, receiver, power)
systemctl list-timers dmr-mapper-snapshot.timer
mapper doctor                     # everything at once, including the service and the timer
```

Closing the console never stops a session; sessions end by their time budget, by `x`, or on
undervoltage.

## 3. Updating

```sh
cd ~/Projects/dmr-system-mapper && deploy/deploy.sh
```

It pulls (fast-forward only), reinstalls, and restarts the service. If a session is running it
does **not** restart, and says so: run it again when the session is over, or
`deploy/deploy.sh --force` to stop the session now. `--dry-run` shows every step without doing it.

## 4. Getting data off the Pi

`e` in the console, or `mapper export --all`, writes `map.html`, `map.json`, `map.graphml` and
`systems.txt` to `var/exports/`; the nightly job does the same. From the laptop:

```sh
scp '<user>@<pi>:Projects/dmr-system-mapper/var/exports/*' .
```

The whole database is the newest `var/snapshots/mapper-YYYYmmdd.db`, or make one now with
`mapper snapshot`. Never copy `var/mapper.db` itself while the service runs: it is in WAL mode
and a plain copy can be inconsistent.

## 5. The archive Pi

The archive pulls; the mapper never logs in to the archive. On the **archive Pi**:

```sh
ssh-keygen -t ed25519 -f ~/.ssh/dmr_mapper_archive -N "" -C dmr-mapper-archive
cat ~/.ssh/dmr_mapper_archive.pub
```

On the **mapper Pi**, add that public key to `~/.ssh/authorized_keys` as **one line**, restricted
to read-only rsync of the snapshot folder (`rrsync` ships with Debian's rsync package):

```
command="/usr/bin/rrsync -ro /home/<user>/Projects/dmr-system-mapper/var/snapshots",restrict ssh-ed25519 AAAA... dmr-mapper-archive
```

With this key the archive can read that one folder and nothing else: no shell, no writes, no
port forwarding.
Check that `rrsync` is there with `ls /usr/bin/rrsync` (Debian 12 and later ship it in the rsync package).

Back on the **archive Pi**, copy `deploy/archive-pull.sh` there (or clone the repository), test it,
then schedule it:

```sh
./archive-pull.sh <user>@<mapper-pi>: /srv/dmr-mapper          # first run; "<mapper-pi>" is its Tailscale name
crontab -e
# every 6 hours; the mapper is often off or out of reach in the field, rsync copies only what is new
17 */6 * * * $HOME/archive-pull.sh <user>@<mapper-pi>: /srv/dmr-mapper >> $HOME/dmr-mapper-archive.log 2>&1
```

It keeps the newest 14 daily snapshots plus 8 older Sunday ones (arguments 3 and 4 change that),
checks every pulled file with `PRAGMA quick_check`, and sets a damaged one aside as `*.bad`.

## 6. Claude Code (MCP)

`mapper serve-mcp` lets Claude Code on the laptop read the map and ask for sessions. It is a
second service, `dmr-mapper-mcp`, beside the daemon: it reads the database and posts commands, and
never opens the receiver. It listens on the Pi's Tailscale address only, without a token: the
tailnet is the trust boundary. Anything but a Tailscale (100.64.0.0/10) or loopback address is
refused.

On the **Pi**, in `config.local.yaml` (`tailscale ip -4` gives the address):

```yaml
mcp:
  enabled: true
  bind: 100.x.y.z                  # the Pi's Tailscale address
  allowed_hosts: [pi5]             # optional: other names the laptop uses for the Pi (MagicDNS)
```

then `deploy/deploy.sh`. It installs the `mcp` extra, renders and enables `dmr-mapper-mcp.service`,
and restarts it on every deploy (always safe: it holds no receiver). `mapper doctor` shows the URL.
With `enabled: false` again, the next deploy stops and disables it.

On the **laptop**, in the folder you run Claude Code from:

```sh
claude mcp add --transport http dmr-mapper http://<pi-tailscale-ip>:8765/mcp
```

or check a `.mcp.json` into that folder (never into this repository: the address is machine-specific):

```json
{ "mcpServers": { "dmr-mapper": { "type": "http", "url": "http://<pi-tailscale-ip>:8765/mcp" } } }
```

`/mcp` in Claude Code shows whether it is connected.

What Claude may do:

| | Tools | Needs |
|---|---|---|
| look | `get_status`, `list_carriers`, `get_fingerprint`, `list_survey_hits`, `query_systems`, `explain_link`, `get_session` | nothing |
| propose | `propose_link`, `withdraw_proposal`, `correlate_now` | nothing; a proposal is evidence with source `mcp`, capped at `mcp.max_proposal_confidence` (0.8), never confirms a link by itself and never lifts the Tier II cap |
| scan | `scan_band`, `decode_carriers`, `watch_slice` | your approval, each time |
| stop | `stop_session` | nothing (it only hands the receiver back) |

Approval (`mcp.approval`):

- `ask` (default): Claude Code shows a yes/no dialog with what, where and how long. You answer it,
  not the model. If the client cannot show one, the request waits for the console.
- `console`: the request always waits on the Pi. The console's status bar shows
  `Claude asks #N: watch 469.3500 MHz 20 min`; `y` approves, `v` refuses. From the shell:
  `mapper requests`, `mapper approve [N]`, `mapper refuse [N]`.
- `none`: no approval here; Claude Code's own permission prompt for the tool is the only gate.

A request nobody answers expires after `mcp.approval_timeout_min` (10), and the daemon refuses an
approved one that has waited much longer (it was down): the place and the question have moved on.
Claude asks for at most `mcp.max_session_minutes` (30), `default_session_minutes` (10) when it gives
no budget, and nothing while another session runs.

Radio IDs reach Claude as pseudonyms (`radio-3f2a9c`, stable while the server runs, so "the same
radio on both carriers" still shows); talkgroups are shown. `mcp.show_radio_ids: true` shows the IDs.

```sh
journalctl -u dmr-mapper-mcp -f                 # requests and errors
curl -s -o /dev/null -w '%{http_code}\n' http://<pi-tailscale-ip>:8765/mcp   # a status (400) = listening; 421 = that name is not in allowed_hosts
```

## 7. Debugging decodes

`decode.keep_raw_logs: true` in `config.local.yaml` (then `sudo systemctl restart dmr-mapper`)
keeps DSD-FME's raw output per carrier and slice in `var/raw/` as
`dsd_fme_<freqHz>_<date>_<time>.log.gz`, compressed, rotated at `decode.raw_log_max_mb` and capped
at `raw_log_max_mb × raw_log_keep` in total (250 MB by default), oldest deleted first. These files
replay with `mapper ingest var/raw/`. Turn it off again afterwards; nothing else is ever kept raw,
and IQ is never kept.

## 8. Restoring from a snapshot

```sh
sudo systemctl stop dmr-mapper
cp var/snapshots/mapper-YYYYmmdd.db var/mapper.db && rm -f var/mapper.db-wal var/mapper.db-shm
sudo systemctl start dmr-mapper
```

## 9. Removing

```sh
sudo systemctl disable --now dmr-mapper dmr-mapper-snapshot.timer dmr-mapper-mcp
sudo rm /etc/systemd/system/dmr-mapper.service /etc/systemd/system/dmr-mapper-snapshot.{service,timer} /etc/systemd/system/dmr-mapper-mcp.service
sudo systemctl daemon-reload
rm ~/.local/bin/mapper
```
