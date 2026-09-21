# Upload Portal

A private file exchange running on a Raspberry Pi. One direction takes files
in through an on-demand link, the other hands them back out with a preview.

```
receiving   iPhone ─► upload.t1mo.dev ──► up.t1mo.dev ─┐
                      GitHub Pages        API calls    │
                                                       ├─► Cloudflare Tunnel ─► Pi :8090
sharing     anyone  ─► up.t1mo.dev/s/<token> ──────────┘
                      viewer page served by the Pi
```

The upload page is static and holds no secrets. The share pages are rendered
by the Pi, because a messenger asking for a link preview only sees the server
response — it cannot run the page, and it never sends the URL fragment, so
Open Graph tags have to come from the server.

## Receiving a file

Opening a session mints a token and prints the link to hand out:

```bash
docker compose exec upload-portal python portalctl.py open --files 1 --max-size 2G
# https://upload.t1mo.dev/#<token>
```

The token sits in the URL fragment, which browsers never send to a server, so
GitHub Pages never sees it. `GET /api/status` without a valid token answers a
flat `{"active": false}`, so polling reveals neither that a session exists nor
what it allows. The session closes itself once the allowed uploads are used
up and discards the token, which makes the old link worthless.

Uploads are chunked at 32 MB. Cloudflare caps a proxied request body at 100 MB
on the free plan and tunnel traffic is always proxied, so phone videos would
otherwise fail with a 413 before reaching the Pi.

## Sharing a file back out

```bash
docker compose exec upload-portal python portalctl.py list
docker compose exec upload-portal python portalctl.py share urlaub.jpg --days 7
# https://up.t1mo.dev/s/<token>
docker compose exec upload-portal python portalctl.py shares     # who opened what
docker compose exec upload-portal python portalctl.py unshare <token>
```

What the recipient sees depends on the file:

| Kind | Presentation |
| --- | --- |
| video | player with scrubbing, double-tap seek, fullscreen, poster frame |
| image | full-bleed view, tap to hide the chrome |
| HEIC/HEIF | converted to JPEG first, since only Safari shows HEIC |
| audio | player card |
| PDF | embedded |
| text, code, csv | first 256 KB, monospace |
| anything else | download card |

Videos are served with byte ranges. This is not an optimisation: Safari
refuses to play a video at all unless the server answers a range request
with 206, and seeking depends on it everywhere.

A browser can only play what it can decode. `.mkv`, `.avi` and friends get a
download card rather than a dead player, and there is no transcoding — a Pi 4
would take hours over it.

## Limits are enforced on the Pi

A page cannot enforce anything a direct `curl -T` would skip, so the service
checks every limit itself: the token on each request, the declared size
against the session maximum, the bytes actually received against the declared
size, the chunk order, and the remaining upload count. Filenames are reduced
to a safe basename and prefixed with a timestamp so nothing overwrites
anything. The timestamp is stripped again for display, so recipients see
`urlaub.jpg` rather than the bookkeeping.

## Setup on the Pi

Uploads must not land on the SD card — it wears out under write load and is
nearly full. `docker-compose.yml` mounts the USB disk instead:

```yaml
volumes:
  - /home/twiese/work/nextcloud-pi/data/upload-portal:/data
```

That path sits inside the Nextcloud data tree because that is where the disk
is mounted. Move the host side if Nextcloud is ever put back into service.

```bash
docker compose up -d --build
curl -s localhost:8090/healthz                                  # {"ok":true}
docker compose exec upload-portal python test_portal.py         # 32 checks
```

The Cloudflare Tunnel is dashboard-managed, so there is no `config.yml` on the
Pi. Its public hostname lives under Networks → Tunnels → Configure → published
application routes: `up` · `t1mo.dev` · HTTP · `localhost:8090`.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/status?t=` | what this session token allows |
| `POST` | `/api/upload/init?t=` | reserve a slot, returns `uploadId` |
| `PUT` | `/api/upload/part?t=&id=&i=` | one chunk, in order |
| `POST` | `/api/upload/done?t=&id=` | finalise, count it, maybe close |
| `GET` | `/s/{token}` | viewer page with Open Graph tags |
| `GET` | `/s/{token}/meta` | what the viewer needs to pick a presentation |
| `GET` | `/s/{token}/file` | the file, with byte ranges |
| `GET` | `/s/{token}/preview` | browser-safe rendition of an image |
| `GET` | `/s/{token}/thumb` | small JPEG for the preview card and poster |
| `GET` | `/s/{token}/dl` | same file as an attachment |
| `GET` | `/healthz` | container health check |

Thumbnails need Pillow, pillow-heif and ffmpeg, all of which the image
installs. If any of them fails, previews degrade to a download card rather
than breaking the page.

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

Note for anyone editing DNS: creating a subdomain through Strato's panel
points it at Strato's own webspace instead of the target, which is why
`upload` has to be an explicit CNAME.
