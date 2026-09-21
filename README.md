# Upload Portal

A private file exchange running on a Raspberry Pi. One direction takes files
in through an on-demand link, the other hands them back out with a preview.

```
you         browser ─► up.t1mo.dev/admin ──────────────┐
                       dashboard, password             │
                                                       │
receiving   iPhone ──► upload.t1mo.dev ──► API calls ──┤── Cloudflare Tunnel ─► Pi :8090
                       GitHub Pages                    │
                                                       │
sharing     anyone ──► up.t1mo.dev/s/<token> ──────────┘
                       viewer page served by the Pi
```

The upload page is static and holds no secrets. The share pages are rendered
by the Pi, because a messenger asking for a link preview only sees the server
response — it cannot run the page, and it never sends the URL fragment, so
Open Graph tags have to come from the server.

## The dashboard

Everything runs from `https://up.t1mo.dev/admin` — on a phone, add it to the
home screen and it behaves like an app. Nothing here needs a terminal.

- **Send someone a file.** Pick it, watch it upload, and the share sheet opens
  by itself with the link already made. On iOS that is the native sheet, so it
  goes straight into WhatsApp or Messages.
- **Have someone send you a file.** One button mints the one-shot upload link
  and offers it for sharing.
- **Manage what is there.** Every file lists with a thumbnail. `⋯` reveals the
  link, a way to revoke it, and delete.

The dashboard is locked behind a password, because it can upload, publish and
delete. Set one before the first start:

```bash
cd ~/upload-portal
echo "PORTAL_ADMIN_PASSWORD=$(openssl rand -base64 18)" > .env
cat .env          # note it down, this is your login
docker compose up -d --build
```

`.env` is gitignored and never leaves the Pi. Without it the service starts but
refuses to open the dashboard at all. The login sets a signed, HttpOnly cookie
that lasts a month, and repeated wrong guesses are throttled.

## Links, and what they are worth

An upload link carries its token in the URL fragment, which browsers never send
to a server, so GitHub Pages never sees it. `GET /api/status` without a valid
token answers a flat `{"active": false}`, so polling reveals neither that a
session exists nor what it allows. The session closes itself once the allowed
uploads are used up, which makes the old link worthless.

A share link carries its token in the path, because the Pi has to read it to
render the page. Anyone holding one can fetch that file until you revoke it.

Uploads are chunked at 32 MB. Cloudflare caps a proxied request body at 100 MB
on the free plan and tunnel traffic is always proxied, so phone videos would
otherwise fail with a 413 before reaching the Pi.

## What the recipient of a share sees

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

## The same things from a terminal

```bash
docker compose exec upload-portal python portalctl.py open --files 1 --max-size 2G
docker compose exec upload-portal python portalctl.py list
docker compose exec upload-portal python portalctl.py share urlaub.jpg --days 7
docker compose exec upload-portal python portalctl.py shares
docker compose exec upload-portal python portalctl.py unshare <token>
docker compose exec upload-portal python portalctl.py close
```

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
echo "PORTAL_ADMIN_PASSWORD=$(openssl rand -base64 18)" > .env
docker compose up -d --build
curl -s localhost:8090/healthz                                  # {"ok":true}
docker compose exec upload-portal python test_portal.py         # 52 checks
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
| `GET` | `/admin` | dashboard, password protected |
| `POST` | `/admin/api/login` · `logout` | session cookie |
| `GET` | `/admin/api/files` | what is on the disk, with share state |
| `POST` | `/admin/api/share` · `unshare` · `delete` | manage a file |
| `POST` | `/admin/api/inbox` · `inbox/close` | the one-shot upload link |

Signed in, the upload endpoints accept the cookie instead of a session token,
and an upload of your own does not spend a slot meant for someone else.

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
