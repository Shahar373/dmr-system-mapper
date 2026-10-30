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
| `dmr-mapper-snapshot.service` | correlate, `mapper snapshot` (to `var/snapshots/`), `mapper export --all` (to `var/exports/`) |

It also links `~/.local/bin/mapper`, so `mapper` works from any folder without activating the
venv (after the next login, if `~/.local/bin` was not on the PATH yet).

Recommended `config.local.yaml` next to `config.example.yaml`:

```yaml
power:
  state_file: /run/dmr/power.json    # the power guard's state file, if that service is installed
```

## 2. Daily use

```sh
mapper tui                        # the console: s survey, d decode, x stop, c correlate, e export, q quit
mapper status                     # one-shot summary
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

## 6. Debugging decodes

`decode.keep_raw_logs: true` in `config.local.yaml` (then `sudo systemctl restart dmr-mapper`)
keeps DSD-FME's raw output per carrier and slice in `var/raw/` as
`dsd_fme_<freqHz>_<date>_<time>.log.gz`, compressed, rotated at `decode.raw_log_max_mb` and capped
at `raw_log_max_mb × raw_log_keep` in total (250 MB by default), oldest deleted first. These files
replay with `mapper ingest var/raw/`. Turn it off again afterwards; nothing else is ever kept raw,
and IQ is never kept.

## 7. Restoring from a snapshot

```sh
sudo systemctl stop dmr-mapper
cp var/snapshots/mapper-YYYYmmdd.db var/mapper.db && rm -f var/mapper.db-wal var/mapper.db-shm
sudo systemctl start dmr-mapper
```

## 8. Removing

```sh
sudo systemctl disable --now dmr-mapper dmr-mapper-snapshot.timer
sudo rm /etc/systemd/system/dmr-mapper.service /etc/systemd/system/dmr-mapper-snapshot.{service,timer}
sudo systemctl daemon-reload
rm ~/.local/bin/mapper
```
