# Set up kf-boot on Ubuntu 24.04 -- Single Host (not very practical)

This guide walks through running a **`3-of-4`** witness threshold on **one machine** for **KERI** (Key Event Receipt Infrastructure) learning or demos: **four** separate witness processes, **one** watcher, and **one** boot service, with **Circus** supervising app processes, **systemd** starting Circus, and **nginx** terminating TLS in front of **kf-boot**.

**What you are installing**

- **[kf-boot](https://github.com/keri-foundation/kf-boot)** — HTTP service that exposes health, bootstrap configuration, onboarding, and account APIs for KERI clients. In this layout it listens on **loopback** only; nginx serves the public HTTPS URLs.
- **`witopnet`** (package **[witness-hk](https://github.com/keri-foundation/witness-hk)**) — KERI **witness** process. Each instance has its own config directory, database (**`--base`**), **boot** port (operator API), and **HTTP** port (client-facing URLs in `witopnet.json`). Here you run **four** instances to mimic a **3-of-4** pool on a single host.
- **`watopnet`** (package **[watcher-hk](https://github.com/keri-foundation/watcher-hk)**) — KERI **watcher** process. **kf-boot** talks to its **boot** URL (`KF_BOOT_WAT_BOOT_URL`) and advertises its public HTTP URL (`KF_BOOT_WAT_PUBLIC_URL`) to clients.

**Scope:** This layout is for **demonstrations, lab work, etc**. It does **not** provide geographic or operator separation; production should use **four hosts** (or more).

**Startup order:** all **`witopnet`** instances → **`watopnet`** → **`kf-boot`**. **kf-boot** must reach every witness **boot** URL in **`KF_BOOT_WITNESS_BACKENDS`** before onboarding allocates a pool.

---

## Port plan (single host)

| Role | Instance | Witness boot (loopback) | Witness HTTP (0.0.0.0) |
|------|----------|-------------------------|-------------------------|
| Witness | wit-1 | 5631 | 5632 |
| Witness | wit-2 | 5641 | 5642 |
| Witness | wit-3 | 5651 | 5652 |
| Witness | wit-4 | 5661 | 5662 |
| Watcher | (single) | 7631 | 7632 |
| kf-boot | — | — | 9723 (loopback; nginx → 443) |

Keep every witness **boot** port on **`127.0.0.1`** (bind via **`--boothost`**). Witness **HTTP** listens on **`0.0.0.0`** so LAN clients can reach the host by IP or hostname.

---

## 1. Baseline packages

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y libsodium-dev build-essential curl git nginx
```

Optional **certbot**:

```bash
sudo apt install -y certbot python3-certbot-nginx
```

---

## 2. Service user and layout

Create the **`keri`** user, a **shared** witness venv parent, **four** witness config trees (one per backend), watcher and kf-boot dirs, and log directory:

```bash
sudo useradd -r -m -d /opt/keri -s /usr/sbin/nologin keri 2>/dev/null || true
sudo mkdir -p /opt/keri/witness/venv \
  /opt/keri/witness-pool/wit-{1,2,3,4}/config/keri/cf/main \
  /opt/keri/watcher/{config/keri/cf/main,venv} \
  /opt/keri/kfboot/{venv,var,keri} \
  /opt/keri/bin /var/log/keri
sudo chown -R keri:keri /opt/keri /var/log/keri
```

Paths used below:

```text
/opt/keri/witness/venv/                    # shared witopnet virtualenv
/opt/keri/witness-pool/wit-1/config/...    # wit-1 KERI config + data (via --base)
/opt/keri/witness-pool/wit-2/config/...
/opt/keri/witness-pool/wit-3/config/...
/opt/keri/witness-pool/wit-4/config/...
/opt/keri/watcher/config/keri/cf/main/watopnet.json
/opt/keri/kfboot/venv/
/opt/keri/kfboot/var/                      # KF_BOOT_DB_PATH
/opt/keri/kfboot/keri/                     # KF_BOOT_KERI_DIR
/opt/keri/bin/run-wit-1.sh … run-wit-4.sh
/opt/keri/bin/run-watopnet.sh
/opt/keri/bin/run-kf-boot.sh
/var/log/keri/
```

Convention: run commands as **`keri`** with **`sudo -Hu keri`**; **`uv`** on **`PATH`** via **`/opt/keri/.local/bin`**.

---

## 3. uv and Python 3.14.0

```bash
sudo -Hu keri bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
sudo -Hu keri bash -c 'export PATH="/opt/keri/.local/bin:$PATH" && cd /opt/keri && uv python install 3.14.0'
```

---

## 4. Install witness-hk, watcher-hk, and kf-boot

**Witness:** one venv under **`/opt/keri/witness/venv`** (all four processes use the same **`witopnet`** binary).

**Watcher / kf-boot:** one venv each under **`/opt/keri/watcher`** and **`/opt/keri/kfboot`**.

### 4a. From Git (typical)

Each line creates the venv, installs **`keri`** from **keripy** Git, then installs the HK package.

```bash
sudo -Hu keri bash -c 'export PATH="/opt/keri/.local/bin:$PATH" && cd /opt/keri/witness && uv venv --python 3.14.0 venv && . venv/bin/activate && uv pip install "keri @ git+https://github.com/WebOfTrust/keripy.git" && uv pip install git+https://github.com/keri-foundation/witness-hk.git'

sudo -Hu keri bash -c 'export PATH="/opt/keri/.local/bin:$PATH" && cd /opt/keri/watcher && uv venv --python 3.14.0 venv && . venv/bin/activate && uv pip install "keri @ git+https://github.com/WebOfTrust/keripy.git" && uv pip install git+https://github.com/keri-foundation/watcher-hk.git'

sudo -Hu keri bash -c 'export PATH="/opt/keri/.local/bin:$PATH" && cd /opt/keri/kfboot && uv venv --python 3.14.0 venv && . venv/bin/activate && uv pip install "keri @ git+https://github.com/WebOfTrust/keripy.git" && uv pip install git+https://github.com/keri-foundation/kf-boot.git'
```

*All four witnesses share one venv at **`/opt/keri/witness/venv`**; watcher and **kf-boot** each have their own venv under **`/opt/keri/watcher`** and **`/opt/keri/kfboot`**. To install **kf-boot** from a local checkout, use **`uv pip install -e /path/to/kf-boot`** in the **kf-boot** venv after **`keri`** is installed.*

---

## 5. Circus

```bash
sudo mkdir -p /opt/circus && sudo chown keri:keri /opt/circus
sudo -Hu keri bash -c 'export PATH="/opt/keri/.local/bin:$PATH" && cd /opt/circus && uv venv --python 3.14.0 venv && . venv/bin/activate && uv pip install circus'
```

---

## 6. KERI config files (`witopnet` × 4 + `watopnet`)

`--config-dir` is the directory **above** `keri/cf/`. This guide uses **`--base`** values **`pool-1`** … **`pool-4`** so each instance’s LMDB and keystores stay separate under that config dir.

Create **`witopnet.json`** for each witness. **`curls`** must match the **`public_url`** you use in **`KF_BOOT_WITNESS_BACKENDS`** for that backend.

**LAN demo (HTTP, replace `192.0.2.10` with your server’s reachable address):**

`/opt/keri/witness-pool/wit-1/config/keri/cf/main/witopnet.json`:

```json
{
  "dt": "2026-01-01T00:00:00.000000+00:00",
  "witopnet": {
    "dt": "2026-01-01T00:00:00.000000+00:00",
    "curls": ["http://192.0.2.10:5632/"]
  }
}
```

`/opt/keri/witness-pool/wit-2/config/keri/cf/main/witopnet.json` — port **5642**:

```json
{
  "dt": "2026-01-01T00:00:00.000000+00:00",
  "witopnet": {
    "dt": "2026-01-01T00:00:00.000000+00:00",
    "curls": ["http://192.0.2.10:5642/"]
  }
}
```

`/opt/keri/witness-pool/wit-3/config/keri/cf/main/witopnet.json` — **5652**; **`wit-4`** — **5662** (same pattern).

**Loopback-only (wallet on the same host):** you may set each **`curls`** to **`http://127.0.0.1:<port>/`** and use the same URLs in **`KF_BOOT_WITNESS_BACKENDS`** **`public_url`** fields; remote wallets will not resolve those.

**Watcher:** `/opt/keri/watcher/config/keri/cf/main/watopnet.json` — set **`watopnet.curls`** to the watcher’s public HTTP URL on **7632** (same **`192.0.2.10`** substitution as the witnesses, or **`127.0.0.1`** for loopback-only demos).

```json
{
  "dt": "2026-01-01T00:00:00.000000+00:00",
  "watopnet": {
    "dt": "2026-01-01T00:00:00.000000+00:00",
    "curls": ["http://192.0.2.10:7632/"]
  }
}
```

**Address binding note (used in launch scripts below):**

- Each witness **boot** port: **`--boothost 127.0.0.1`** (management API not on the public interface).
- Each witness **HTTP** port: **`--host 0.0.0.0`** so clients can connect to **`curls`**.
- Watcher: **`--boothost 127.0.0.1`** and **`--bootport 7631`** for the boot API; **`--host 0.0.0.0`** and **`--http 7632`** for client HTTP (must match **`watopnet.curls`**).

---

## 7. Launch scripts

Script names **`run-wit-N.sh`** match the **`wit-N`** pool directories under **`/opt/keri/witness-pool/`**.

### `/opt/keri/bin/run-wit-1.sh` … **`run-wit-4.sh`**

Only **ports**, **`--base`**, **`--config-dir`**, and **log file** differ.

**`run-wit-1.sh`:**

```bash
#!/usr/bin/env bash
set -euo pipefail
export KERI_BASER_MAP_SIZE=1099511627776
exec /opt/keri/witness/venv/bin/witopnet marshal start \
  --config-dir /opt/keri/witness-pool/wit-1/config \
  --base pool-1 \
  --host 0.0.0.0 \
  --http 5632 \
  --boothost 127.0.0.1 \
  --bootport 5631 \
  --loglevel INFO \
  --logfile /var/log/keri/wit-1.log
```

**`run-wit-2.sh`:**

```bash
#!/usr/bin/env bash
set -euo pipefail
export KERI_BASER_MAP_SIZE=1099511627776
exec /opt/keri/witness/venv/bin/witopnet marshal start \
  --config-dir /opt/keri/witness-pool/wit-2/config \
  --base pool-2 \
  --host 0.0.0.0 \
  --http 5642 \
  --boothost 127.0.0.1 \
  --bootport 5641 \
  --loglevel INFO \
  --logfile /var/log/keri/wit-2.log
```

**`run-wit-3.sh`:**

```bash
#!/usr/bin/env bash
set -euo pipefail
export KERI_BASER_MAP_SIZE=1099511627776
exec /opt/keri/witness/venv/bin/witopnet marshal start \
  --config-dir /opt/keri/witness-pool/wit-3/config \
  --base pool-3 \
  --host 0.0.0.0 \
  --http 5652 \
  --boothost 127.0.0.1 \
  --bootport 5651 \
  --loglevel INFO \
  --logfile /var/log/keri/wit-3.log
```

**`run-wit-4.sh`:**

```bash
#!/usr/bin/env bash
set -euo pipefail
export KERI_BASER_MAP_SIZE=1099511627776
exec /opt/keri/witness/venv/bin/witopnet marshal start \
  --config-dir /opt/keri/witness-pool/wit-4/config \
  --base pool-4 \
  --host 0.0.0.0 \
  --http 5662 \
  --boothost 127.0.0.1 \
  --bootport 5661 \
  --loglevel INFO \
  --logfile /var/log/keri/wit-4.log
```

### `/opt/keri/bin/run-watopnet.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail
export KERI_BASER_MAP_SIZE=1099511627776
exec /opt/keri/watcher/venv/bin/watopnet start \
  --config-dir /opt/keri/watcher/config \
  --host 0.0.0.0 \
  --http 7632 \
  --boothost 127.0.0.1 \
  --bootport 7631 \
  --loglevel INFO \
  --logfile /var/log/keri/watopnet.log
```

### `/opt/keri/bin/run-kf-boot.sh`

Use **`KF_BOOT_WITNESS_BACKENDS`** and **do not** set **`KF_BOOT_WIT_BOOT_URL`** or **`KF_BOOT_WIT_PUBLIC_URL`**. **`boot_url`** values are how **kf-boot** reaches each witness **boot** API from **this host** (loopback). **`public_url`** must match each instance’s **`witopnet.curls`** (what wallets and allocation payloads use).

Replace **`192.0.2.10`** with the same address you put in **`curls`**. Replace **`boot.example.com`** with your nginx vhost for **kf-boot** if you terminate TLS.

```bash
#!/usr/bin/env bash
set -euo pipefail
export KERI_BASER_MAP_SIZE=1099511627776
export KF_BOOT_HOST=127.0.0.1
export KF_BOOT_PORT=9723
export KF_BOOT_DB_PATH=/opt/keri/kfboot/var
export KF_BOOT_KERI_DIR=/opt/keri/kfboot/keri
export KF_BOOT_WITNESS_BACKENDS="wit-1|http://127.0.0.1:5631|http://192.0.2.10:5632,wit-2|http://127.0.0.1:5641|http://192.0.2.10:5642,wit-3|http://127.0.0.1:5651|http://192.0.2.10:5652,wit-4|http://127.0.0.1:5661|http://192.0.2.10:5662"
export KF_BOOT_WAT_BOOT_URL=http://127.0.0.1:7631
export KF_BOOT_WAT_PUBLIC_URL=http://192.0.2.10:7632
export KF_BOOT_ONBOARDING_PUBLIC_URL=https://boot.example.com/onboarding
export KF_BOOT_ACCOUNT_PUBLIC_URL=https://boot.example.com/account
exec /opt/keri/kfboot/venv/bin/kf-boot
```

For **HTTPS** witness/watcher **`public_url`** values, align **`curls`** and nginx (or TLS termination) accordingly—**kf-boot** does not require HTTP; it requires consistency between **`public_url`** and what you advertise in **`witopnet.json`**.

```bash
sudo chmod 750 /opt/keri/bin/run-wit-*.sh /opt/keri/bin/run-watopnet.sh /opt/keri/bin/run-kf-boot.sh
sudo chown keri:keri /opt/keri/bin/run-wit-*.sh /opt/keri/bin/run-watopnet.sh /opt/keri/bin/run-kf-boot.sh
```

---

## 8. Circus configuration

Four witness processes (**`witopnet`**) start **before** **`watopnet`**, then **`kf-boot`**. Higher Circus **`priority`** values start first.

`/etc/circus/circus.ini`:

```ini
[circus]
check_delay = 5
endpoint = tcp://127.0.0.1:5555
pubsub_endpoint = tcp://127.0.0.1:5556
statsd = false

[watcher:wit-1]
cmd = /opt/keri/bin/run-wit-1.sh
numprocesses = 1
priority = 25
working_dir = /opt/keri/witness-pool/wit-1
user = keri
copy_env = True
rlimit_nofile = 65536
stdout_stream.class = FileStream
stdout_stream.filename = /var/log/keri/wit-1-circus.stdout.log
stderr_stream.class = FileStream
stderr_stream.filename = /var/log/keri/wit-1-circus.stderr.log

[watcher:wit-2]
cmd = /opt/keri/bin/run-wit-2.sh
numprocesses = 1
priority = 24
working_dir = /opt/keri/witness-pool/wit-2
user = keri
copy_env = True
rlimit_nofile = 65536
stdout_stream.class = FileStream
stdout_stream.filename = /var/log/keri/wit-2-circus.stdout.log
stderr_stream.class = FileStream
stderr_stream.filename = /var/log/keri/wit-2-circus.stderr.log

[watcher:wit-3]
cmd = /opt/keri/bin/run-wit-3.sh
numprocesses = 1
priority = 23
working_dir = /opt/keri/witness-pool/wit-3
user = keri
copy_env = True
rlimit_nofile = 65536
stdout_stream.class = FileStream
stdout_stream.filename = /var/log/keri/wit-3-circus.stdout.log
stderr_stream.class = FileStream
stderr_stream.filename = /var/log/keri/wit-3-circus.stderr.log

[watcher:wit-4]
cmd = /opt/keri/bin/run-wit-4.sh
numprocesses = 1
priority = 22
working_dir = /opt/keri/witness-pool/wit-4
user = keri
copy_env = True
rlimit_nofile = 65536
stdout_stream.class = FileStream
stdout_stream.filename = /var/log/keri/wit-4-circus.stdout.log
stderr_stream.class = FileStream
stderr_stream.filename = /var/log/keri/wit-4-circus.stderr.log

[watcher:watopnet]
cmd = /opt/keri/bin/run-watopnet.sh
numprocesses = 1
priority = 21
working_dir = /opt/keri/watcher
user = keri
copy_env = True
rlimit_nofile = 65536
stdout_stream.class = FileStream
stdout_stream.filename = /var/log/keri/watopnet-circus.stdout.log
stderr_stream.class = FileStream
stderr_stream.filename = /var/log/keri/watopnet-circus.stderr.log

[watcher:kf-boot]
cmd = /opt/keri/bin/run-kf-boot.sh
numprocesses = 1
priority = 10
working_dir = /opt/keri/kfboot
user = keri
copy_env = True
rlimit_nofile = 65536
stdout_stream.class = FileStream
stdout_stream.filename = /var/log/keri/kf-boot-circus.stdout.log
stderr_stream.class = FileStream
stderr_stream.filename = /var/log/keri/kf-boot-circus.stderr.log
```

```bash
sudo mkdir -p /etc/circus
sudo chown root:keri /etc/circus/circus.ini
sudo chmod 640 /etc/circus/circus.ini
```

---

## 9. systemd unit for Circus

Create **`/etc/systemd/system/circus.service`** (root-owned **`644`** is typical):

```ini
[Unit]
Description=Circus process manager (four witopnet, watopnet, kf-boot)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=keri
Group=keri
ExecStart=/opt/circus/venv/bin/circusd /etc/circus/circus.ini
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now circus
sudo systemctl status circus
```

---

## 10. nginx: TLS and reverse proxy to kf-boot

**kf-boot** listens on **`127.0.0.1:9723`**. nginx terminates TLS on **443** and proxies **`/health`**, **`/bootstrap/config`**, **`/onboarding`**, and **`/account`** to that upstream. For **HTTPS** witness or watcher URLs, add your own **`server`** / **`location`** blocks (or another proxy) so **`witopnet.curls`** / **`watopnet.curls`** and **`KF_BOOT_*_PUBLIC_URL`** stay consistent.

Create a webroot for ACME HTTP-01 (if you use it):

```bash
sudo mkdir -p /var/www/certbot
```

Obtain certificates (example with **certbot** and **`boot.example.com`**):

```bash
sudo certbot certonly --webroot -w /var/www/certbot -d boot.example.com
```

Create **`/etc/nginx/sites-available/kf-boot.conf`** (replace **`boot.example.com`** and certificate paths):

```nginx
upstream kf_boot {
    server 127.0.0.1:9723;
    keepalive 8;
}

server {
    listen 80;
    listen [::]:80;
    server_name boot.example.com;

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 301 https://boot.example.com$request_uri;
    }
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name boot.example.com;

    ssl_certificate     /etc/letsencrypt/live/boot.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/boot.example.com/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    client_max_body_size 64m;

    location = /health {
        proxy_pass http://kf_boot;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";
    }

    location = /bootstrap/config {
        proxy_pass http://kf_boot;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";
    }

    location ^~ /onboarding {
        proxy_pass http://kf_boot;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }

    location ^~ /account {
        proxy_pass http://kf_boot;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }

    location / {
        return 404;
    }
}
```

Enable the site and reload nginx:

```bash
sudo ln -sf /etc/nginx/sites-available/kf-boot.conf /etc/nginx/sites-enabled/kf-boot.conf
sudo nginx -t && sudo systemctl reload nginx
```

Comment out **`ssl_dhparam`** if that file does not exist on your system. If this vhost also serves other content at **`/`**, merge these **`location`** blocks into your existing **`server`** and drop or adjust the final **`location /`** block.

---

## 11. Firewall

Allow SSH, HTTP, HTTPS, **all four witness HTTP ports**, watcher HTTP, and optionally **9723** only if you must debug without nginx (normally keep **9723** closed publicly).

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow 5632/tcp
sudo ufw allow 5642/tcp
sudo ufw allow 5652/tcp
sudo ufw allow 5662/tcp
sudo ufw allow 7632/tcp
sudo ufw enable
```

Do **not** expose witness **boot** ports **5631, 5641, 5651, 5661** or watcher **7631** publicly if they stay on **loopback**.

---

## 12. Verification

- **Witness boot (×4):** `curl -i http://127.0.0.1:5631/health` (and **5641**, **5651**, **5661**) → expect healthy responses per your **`witopnet`** build (often **204**).
- **Watcher boot:** `GET /health` is not defined on **`127.0.0.1:7631`** for many **`watopnet`** builds. Use `curl -i http://127.0.0.1:7631/watchers` and expect a non-connection failure (often **405 Method Not Allowed**).
- **kf-boot:** `curl -sS http://127.0.0.1:9723/health` → **`{"status":"ok"}`**; `curl -sS http://127.0.0.1:9723/bootstrap/config` → JSON whose **`bootstrap.account_options`** includes **`3-of-4`** with **`witness_count`** **4** and **`toad`** **3**.
- Through nginx: `https://boot.example.com/health` and **`/bootstrap/config`**.

---

## 13. Operations

- Back up **`/opt/keri`** including **`witness-pool/wit-*/config`** (KERI data under each **`--base`**), **`/opt/keri/watcher/config`**, **`KF_BOOT_DB_PATH`**, **`KF_BOOT_KERI_DIR`**.
- Rotate or cap logs under **`/var/log/keri/`**.
