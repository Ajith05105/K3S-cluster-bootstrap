# E6 — Backup restore: can Gitea actually be recovered?

**Date:** 2026-10-11
**Source:** `/var/backups/gitea/gitea-2026-10-10-0233.tar.gz` on server-1 (1,482,001 bytes)
**Method:** restore into scratch paths on server-1 and verify independently. **Live Gitea was never touched.**
**Cleanup:** all scratch data removed; the three dumps on server-1 are untouched.

Closes the largest gap in this evaluation. Backups have existed since 4 Oct and were known to be *valid*; until now nothing had ever been *restored* from one.

## What we did, and how

Gitea runs on **server-3**; backups are written on server-1 and server-2, which is the arrangement the role is designed to produce. The restore was run on server-1 — i.e. on a node that would survive the failure it protects against.

Five independent checks, each answering a different question:

| Step | Question |
|---|---|
| 1 | Does the archive decompress without error? |
| 2 | Does it extract, and what is in it? |
| 3 | Are the git objects intact? (`git fsck`) |
| 4 | Can the repositories actually be used? (`git clone`) |
| 5 | Does the database load into a working engine? |

## What we measured

### Timings

| Step | Duration |
|---|---|
| `gzip -t` integrity check | **0.1 s** |
| Extract (1.4 MB → 9.4 MB) | **0.1 s** |
| `git clone` of `cluster-config` | **0.1 s** |
| Load `gitea-db.sql` into fresh SQLite | **29.5 s** |
| **Total data-recovery path** | **~30 s** |

### Contents

```
app.ini          1,051 B     config, including secrets
gitea-db.sql     439,995 B   full SQLite schema + data
data/                        Gitea data directory
repos/                       3 bare repositories
```

### Repository integrity — all clean

| Repository | fsck errors | Commits | HEAD |
|---|---|---|---|
| `admin/cluster-config` | **0** | 1 | `e7fdad2` 2026-10-06 |
| `apps/cluster-console` | **0** | 8 | `e1758ff` 2026-10-09 |
| `apps/whoami` | **0** | 4 | `f9a57fc` 2026-10-07 |

`cluster-config` cloned out with its full tree intact — `argocd/values.yaml`, `kube-vip/`, `ansible-runner/`, `gitea/values.yaml` and the whole `app/` chart including `limitrange.yaml` and `resourcequota.yaml`. `cluster-console` restored to **79 files across 8 commits**, matching the live repository exactly.

### Database — fully restorable

Loaded into a fresh SQLite database with no errors:

```
113 tables
  users    2      (admin, apps)
  repos    3      (admin/cluster-config, apps/whoami, apps/cluster-console)
  tokens   1      (platform-pull-token, read:package)
  actions  31
```

## What this proves

**Gitea's git and database state is genuinely recoverable, quickly, from a node that survives the loss.** The whole data-recovery path is ~30 seconds, dominated entirely by SQLite ingest; the git side is effectively instant at this repository size. Every repository passes `git fsck` with zero errors and clones with complete history. The limitation recorded as L1 — *"no backup"* — is obsolete.

That is the good news, and it is real. Two things qualify it.

### The backup does not contain the container images

`gitea dump` runs with `--skip-package-data`, so **none of the registry contents are in the archive.** The backup script's own comment argues this is safe because the images "still exist as tarballs in `/var/lib/rancher/k3s/push-images` on every node." That claim was checked, and it is **half true**:

```
server-1: 12 tarballs, 2.6G     server-2: 12 tarballs, 2.6G     server-3: 12 tarballs, 2.6G
```

Replication is real. But the tarballs hold only the **platform** images — `argocd`, `dex`, `redis` and the nine monitoring images. The **application** images are not there:

| Image | In push-images? | In the backup? | Recoverable? |
|---|---|---|---|
| `argocd`, `dex`, `redis`, 9× monitoring | **Yes** (3× replicated) | No | Yes, via `crane` |
| `apps/cluster-console:0.3.2` | No | No | Only by rebuilding |
| `apps/camera-ingest:0.1.0` | No | No | Only by rebuilding |
| `apps/whoami:v1.12.0` | No | No | Only by rebuilding |
| `apps/nginx:1.31.6-alpine` | No | No | Needs the internet |

So if server-3's SD card dies today: repositories, users and tokens come back in ~30 s, the platform images re-push from any surviving node, and **every application image is gone.** Their *source* survives in the backed-up repositories, so they are rebuildable — but rebuilding needs a build toolchain and, for anything with a public base image, **outbound internet access**. Recovery therefore breaks the air-gap that the rest of the design maintains.

The honest framing: **the backup protects the source of truth, not the artefacts built from it.** For a GitOps cluster that is a defensible place to draw the line, but it must be stated, because "we have backups" currently implies more than it delivers.

### The backups are unencrypted and world-readable

The archive contains Gitea's complete secret material:

```
app.ini:  SECRET_KEY, INTERNAL_TOKEN, JWT_SECRET, LFS_JWT_SECRET
db:       1 user password hash, 1 API token hash (platform-pull-token, read:package)
```

And it is stored like this, on two nodes:

```
-rw-r--r-- root:root  /var/backups/gitea/gitea-2026-10-10-0233.tar.gz
drwxr-xr-x root:root  /var/backups/gitea
```

**Mode 0644 in a 0755 directory, unencrypted.** Any local user on server-1 or server-2 can read the full credential set for the cluster's source of truth, and so can anyone who removes an SD card. In a project whose central claim is sovereignty — keeping data on hardware you control — this is the point where physical possession of the hardware becomes complete compromise of it. A `chmod 0600` on the directory is a one-line mitigation for the first half; encryption at rest would be needed for the second.

### Still not proven

A **running Gitea serving from restored data** was not stood up. This test proves the bytes are complete, consistent and usable; it does not prove that pointing a fresh Gitea deployment at them produces a working service. The remaining risks are configuration-shaped rather than data-shaped — `app.ini` paths, the PVC and `local-path` node pinning — so the next step is a scratch Gitea instance in its own namespace.

### Confirmed: the orphaned `.partial` bug is live

```
gitea-2026-10-06-0232.tar.gz.partial    0 bytes, still present after 5 days
```

Retention globs `gitea-*.tar.gz`, which cannot match `*.partial`, so failed dumps are never pruned. Harmless at 0 bytes; a dump that dies midway would leave a multi-hundred-megabyte orphan that nothing ever removes.

## Reproduction

```bash
SRC=/var/backups/gitea/gitea-2026-10-10-0233.tar.gz
R=/tmp/gitea-restore-test; sudo mkdir -p $R
sudo gzip -t $SRC && sudo tar xzf $SRC -C $R

# repository integrity + usability
for g in $(sudo find $R/repos -name '*.git'); do sudo git --git-dir=$g fsck; done
sudo git clone $R/repos/admin/cluster-config.git /tmp/clone-test

# database (sqlite3 is not installed on the nodes; python3 is)
sudo python3 -c "import sqlite3;sqlite3.connect('/tmp/r.db').executescript(open('$R/gitea-db.sql',errors='ignore').read())"
```

Run this on server-1 or server-2 — **never on the node hosting Gitea**, which is the node the backup exists to survive.
