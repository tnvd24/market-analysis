# Running it in a container

The container needs **no credentials** — prices, corporate actions and filings all come from
NSE unauthenticated. A clean machine can run the whole thing.

**It is portable.** The image is a plain OCI image on `python:3.12-slim`, and the Compose file
sticks to features every implementation supports. **Docker, Podman and Rancher Desktop all
work** — see [§5](#5-podman-rancher-desktop-and-portability) for the one real gotcha (Podman's
rootless UID mapping).

---

## 1. Install a container runtime

You don't have one yet. This needs `sudo`, so run it yourself.

**Docker — CachyOS / Arch** (your box):
```bash
sudo pacman -S docker docker-compose
sudo systemctl enable --now docker
sudo usermod -aG docker $USER      # so you can run docker without sudo
```
Then **log out and back in** (or run `newgrp docker`) for the group change to take effect.

**Podman — Arch** (no daemon, no group, rootless by default):
```bash
sudo pacman -S podman podman-compose
```

**Rancher Desktop:** install from rancherdesktop.io. In *Preferences → Container Engine*,
either engine works; **dockerd (moby)** gives you the `docker` CLI directly.

Check whichever you picked:
```bash
docker run --rm hello-world      # or: podman run --rm hello-world
```

**Ubuntu/Debian:** `curl -fsSL https://get.docker.com | sudo sh`
**macOS / Windows:** Docker Desktop, Rancher Desktop, or Podman Desktop — all fine.

---

## 2. Run the pipeline

From the repo root:

```bash
docker compose run --rm pipeline
```

That builds the image on first use, then runs the whole chain:

```
instruments → prices → actions → adjust → features → news → quality → packs
```

and exits. Your research packs land in **`./packs/`** on the host, and the database and
bhavcopy cache persist in **`./data/`**.

It exits **non-zero if the quality checks find errors** — so it fails loudly rather than
writing packs full of numbers nobody should trust. That's what makes it safe to schedule.

### First run vs. daily runs

The default is **incremental**: it pulls only the days it's missing. On a cold `./data/`, do
the full backfill once (~10-15 minutes, mostly NSE downloads):

```bash
docker compose run --rm pipeline asr pipeline --full --years 3 --out /app/packs
```

After that, the daily run takes a couple of minutes:

```bash
docker compose run --rm pipeline
```

Downloaded days are cached in `./data/bhavcopy/`, so a re-run costs **no network at all**.

---

## 3. Run individual commands

Anything after the service name replaces the default command:

```bash
docker compose run --rm pipeline asr ingest status
docker compose run --rm pipeline asr quality
docker compose run --rm pipeline asr pack build RELIANCE
docker compose run --rm pipeline asr backtest universe --strategy rsi_reversion
```

Or without Compose at all:

```bash
docker build --target runtime -t asr:prod .
docker run --rm -v "$PWD/data:/app/data" -v "$PWD/packs:/app/packs" asr:prod asr ingest status
```

**The volumes matter.** Without `-v ... /app/data`, the container starts with an empty
database every time and re-downloads three years of history on each run.

---

## 4. A dev shell

For poking around, running tests, or working inside the container:

```bash
docker compose up -d shell
docker compose exec shell asr info
docker compose exec shell pytest -q
docker compose exec shell bash
docker compose down                  # when you're done
```

The `shell` service mounts your working tree live, so edits on the host take effect
immediately inside it.

---

## 5. Podman, Rancher Desktop, and portability

Nothing here is Docker-specific. The image is a standard OCI image, and the Compose file
avoids anything exotic — no BuildKit-only syntax, no `env_file: required:` (too new for
podman-compose), just plain `${VAR:-default}` substitution that every implementation
understands.

### Podman

```bash
podman-compose run --rm pipeline
# or, on Podman 4.7+ which speaks the Compose spec directly:
podman compose run --rm pipeline
```

Plain Podman works too — it's argument-compatible with Docker:
```bash
podman build --target runtime -t asr:prod .
podman run --rm -v "$PWD/data:/app/data:U" -v "$PWD/packs:/app/packs:U" asr:prod asr pipeline
```

**The one real gotcha: rootless UID mapping.** Podman rootless maps container UIDs into your
subuid range, so the container's `appuser` (uid 1000) is *not* your host uid 1000. Files it
writes to a bind-mounted `./data` come back owned by something like `100999`, and you can't
edit them. Two fixes, either works:

```bash
# a) let Podman relabel/chown the mount for you — the `:U` suffix
podman run --rm -v "$PWD/data:/app/data:U" asr:prod asr pipeline

# b) or map your own uid into the container
podman run --rm --userns=keep-id -v "$PWD/data:/app/data" asr:prod asr pipeline
```

With `podman-compose`, add `:U` to the volume lines, or run it as `--userns=keep-id`. This
does not affect Docker, which shares the host UID namespace.

On **SELinux** systems (Fedora, RHEL) add `:z` to bind mounts, or the container can't read
them: `-v "$PWD/data:/app/data:z,U"`.

### Rancher Desktop

Works as-is. With the **dockerd (moby)** engine, every `docker` and `docker compose` command
in this document works unchanged. With the **containerd** engine, use `nerdctl` instead:

```bash
nerdctl compose run --rm pipeline
```

Make sure Rancher Desktop has enough memory (Preferences → Virtual Machine): the build
compiles nothing heavy, but `pandas` + `pyarrow` + `duckdb` want ~2 GB during install.

### Kubernetes / Cloud Run

The `runtime` stage is a lean, non-root, single-command image, which is exactly what these
want. `asr pipeline` runs to completion and exits non-zero on failure — the contract a
**Kubernetes `Job`**, a **Cloud Run Job**, or a systemd `oneshot` unit expects. That's Phase
8-9, and it needs no changes to the image.

---

## 6. Schedule it

The pipeline is a plain command that exits non-zero on failure, so any scheduler works.

**cron** (weekdays at 7pm IST, after the market closes and bhavcopy is published):
```cron
0 19 * * 1-5 cd /home/prasad/Projects/Market\ Analysis && docker compose run --rm pipeline >> /var/log/asr.log 2>&1
```

**systemd timer** is tidier if you want retries and status. **Cloud Run Jobs** (Phase 8-9)
takes the same image unchanged — that's the point of keeping the runtime stage lean.

Bhavcopy for a given day appears in the evening, so **run it after ~6pm IST**. If you run it
earlier, the pipeline just finds no new trading day and does nothing — which is safe, but
pointless.

---

## What's in the image

| | |
|---|---|
| Base | `python:3.12-slim` |
| Contains | the `asr` package, the Nifty 500 universe snapshot, `prompts/analysis.md` |
| Does **not** contain | your data, your `.env`, any credential, any dev tooling |
| Runs as | non-root (`appuser`, uid 1000 — so a bind-mounted `./data` stays writable) |
| Default command | `asr pipeline` |

`.dockerignore` keeps `data/`, `.env`, `.git` and caches out of the build context, so the
image carries no secrets and no stale database.

---

## Troubleshooting

**`permission denied` on `/var/run/docker.sock`**
You're not in the `docker` group yet, or haven't logged out and back in since being added.
Test with `newgrp docker`.

**`./data` or `./packs` written as root, host can't edit them**
The image runs as uid 1000, which matches the usual first desktop user. If your uid differs
(`id -u`), pass it: `docker compose run --rm --user "$(id -u):$(id -g)" pipeline`.

**The pipeline exits 1 and says "data-quality errors"**
That's it working. Read what it printed — most likely a corporate action NSE described in a
format the parser can't read, so some stock's prices aren't adjusted. Fix the parser rather
than the symptom. To see the packs anyway (knowing they may be wrong):
`docker compose run --rm pipeline asr pipeline --no-strict --out /app/packs`

**NSE requests fail or hang inside the container**
Same as on the host: NSE rate-limits and is bot-hostile. Just run it again — cached days are
skipped, so it resumes where it stopped.

**The build is slow the first time**
`pandas`, `pyarrow`, `duckdb` and `pandas-ta` are large. Subsequent builds reuse the layer
cache unless `pyproject.toml` changes.
