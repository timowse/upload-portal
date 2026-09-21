# Upload Portal

A private, on-demand upload link. GitHub Pages serves the page, a small
service on the Raspberry Pi receives the files.

```
iPhone ──► upload.t1mo.dev        GitHub Pages, static page only
              │
              └─ API calls ──► up.t1mo.dev ──► Cloudflare Tunnel ──► Pi :8090
```

The page holds no secrets and never touches a file. It asks the Pi what the
session allows, then streams the file there in chunks. Uploads land on the
USB disk.

## How a session works

Opening a session on the Pi mints a fresh token and prints the link to hand
out:

```
https://upload.t1mo.dev/#<token>
```

The token sits in the URL fragment, which browsers never send to a server,
so GitHub Pages never sees it. Only our own API calls carry it, and only to
the Pi. `GET /api/status` without a valid token answers a flat
`{"active": false}`, so polling the endpoint reveals neither that a session
exists nor what it allows.

A session ends by itself once the allowed number of uploads is reached, and
the token is discarded with it. An old link is then worthless.

## Setup on the Pi

The uploads must not go on the SD card — card wear, and it is nearly full.
`docker-compose.yml` mounts the USB disk instead:

```yaml
volumes:
  - /home/twiese/work/nextcloud-pi/data/upload-portal:/data
```

That directory sits inside the Nextcloud data tree because that is where
the disk is mounted. Change the host side of the mount if Nextcloud is ever
put back into service.

```bash
docker compose up -d --build
curl -s localhost:8090/healthz     # {"ok":true}
```

Then add a public hostname to the existing Cloudflare Tunnel, under
Networks → Tunnels → Configure → Public Hostname:

| Field | Value |
| --- | --- |
| Subdomain | `up` |
| Domain | `t1mo.dev` |
| Service | `HTTP` → `localhost:8090` |

Cloudflare creates the DNS record itself. The tunnel is dashboard-managed,
so there is no `config.yml` on the Pi and `cloudflared tunnel route dns`
is not used.

## Daily use

```bash
cd ~/upload-portal
docker compose exec upload-portal python portalctl.py open --files 1 --max-size 2G
docker compose exec upload-portal python portalctl.py status
docker compose exec upload-portal python portalctl.py list
docker compose exec upload-portal python portalctl.py close
```

`open` prints the link. Send it, and it stops working after the upload.

## Limits are enforced on the Pi

The page shows the file count and size limit, but a page cannot enforce
anything that a direct `curl -T` would not skip. So the service checks every
limit itself: the declared size against the session maximum, the bytes
actually received against the declared size, the chunk order, the remaining
upload count, and the token on every single request. Filenames are reduced
to a safe basename and prefixed with a timestamp, so nothing overwrites
anything.

## Why the file is chunked

Cloudflare caps a proxied request body at 100 MB on the free plan, and
tunnel traffic is always proxied. The browser therefore slices the file into
32 MB pieces and the service appends them in order. Phone videos would
otherwise fail with a 413 well before reaching the Pi.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/status?t=` | what this token is allowed to do |
| `POST` | `/api/upload/init?t=` | reserve a slot, returns `uploadId` |
| `PUT` | `/api/upload/part?t=&id=&i=` | one chunk, in order |
| `POST` | `/api/upload/done?t=&id=` | finalise, count it, maybe close |
| `GET` | `/healthz` | container health check |

## DNS

Both names live in the Cloudflare zone for `t1mo.dev`; the domain stays
registered at Strato.

| Name | Record | Proxy |
| --- | --- | --- |
| `upload` | CNAME → `timowse.github.io` | DNS only |
| `up` | created by the tunnel | proxied |

`upload` stays unproxied so GitHub Pages terminates its own TLS. `.dev` is
HSTS-preloaded, so both names only work over HTTPS — there is no plain HTTP
fallback for debugging.

Note for anyone editing DNS here: creating a subdomain through Strato's
panel points it at Strato's own webspace instead of the target, which is
why `upload` has to be an explicit CNAME.
