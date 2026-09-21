# Upload Portal

A public file drop on a Raspberry Pi. Upload something, get a link, send it
on. Files delete themselves after 30 days.

```
upload.t1mo.dev   GitHub Pages ─┐
                                 ├─► API on up.t1mo.dev ─► Cloudflare Tunnel ─► Pi :8090
up.t1mo.dev       the Pi ───────┘

up.t1mo.dev/s/<token>            the preview page for one file
```

Both hostnames serve the same `index.html`. Pages reads `config.json` to find
the API; served from the Pi the file is absent and the API is simply
same-origin. So the page keeps working from either address, and the Pi does
not need Pages to be up.

## No accounts, by choice

Anyone who opens either address can upload, and anyone holding a share link
can fetch that file until it expires. There is no login, because the point is
to hand a link to someone who should not have to make an account.

That also means the service is open to whoever finds it, and it will be
found: `up.t1mo.dev` appears in the public Certificate Transparency logs the
moment Cloudflare issues its certificate, and those logs get scanned. The
limits below are what stands between that and a full disk — they are
operational ceilings, not access control.

| Guard | Default | Setting |
| --- | --- | --- |
| Size per file | 2 GB | `PORTAL_MAX_GB` |
| Lifetime | 30 days | `PORTAL_DAYS` |
| Uploads per address per hour | 30 | `PORTAL_UPLOADS_PER_HOUR` |
| Disk kept free | 5 GB | `PORTAL_KEEP_FREE_GB` |

An upload is refused with 507 once free space would fall below the floor, so
a full disk never takes the rest of the Pi down with it. A sweeper runs hourly
and also removes blobs no index entry claims.

## What the recipient sees

| Kind | Presentation |
| --- | --- |
| video | player with scrubbing, double-tap seek, fullscreen, poster frame |
| image | full-bleed view, tap to hide the chrome |
| HEIC/HEIF | converted to JPEG first, since only Safari shows HEIC |
| audio | player card |
| PDF | embedded |
| text, code, csv | first 256 KB, monospace |
| anything else | download card |

The Pi renders the preview page itself rather than Pages, because a messenger
fetching a link for its preview card only sees the server response — it cannot
run the page, and it never sends a URL fragment. So Open Graph tags have to
come from the server, and sharing a link produces a real card rather than a
bare URL.

Files are served with byte ranges. This is not an optimisation: Safari refuses
to play a video at all unless a range request is answered with 206, and
seeking depends on it everywhere.

A browser can only play what it can decode. `.mkv`, `.avi` and friends get a
download card rather than a dead player, and there is no transcoding — a Pi 4
would take hours over it.

## Uploading is chunked

The browser slices a file into 32 MB pieces and the service appends them in
order. Cloudflare caps a proxied request body at 100 MB on the free plan and
tunnel traffic is always proxied, so phone videos would otherwise fail with a
413 before reaching the Pi.

Names never become paths. Bytes live under `blobs/<id>` with an opaque name
and the name a person sees is metadata, so two people uploading `IMG_0001.HEIC`
do not collide and nothing a caller types reaches the filesystem.

## Setup on the Pi

Uploads must not land on the SD card — it wears out under write load and is
nearly full. `docker-compose.yml` mounts the USB disk instead:

```yaml
volumes:
  - /home/twiese/work/nextcloud-pi/data/upload-portal:/data
```

```bash
docker compose up -d --build
curl -s localhost:8090/healthz                              # {"ok":true}
docker compose exec upload-portal python test_portal.py     # 31 checks
```

The Cloudflare Tunnel is dashboard-managed, so there is no `config.yml` on the
Pi. Its public hostname lives under Networks → Tunnels → Configure → published
application routes: `up` · `t1mo.dev` · HTTP · `localhost:8090`.

Files from the earlier layout under `incoming/` are adopted automatically on
first start and given the standard lifetime.

## Housekeeping

Nothing below is needed for daily use.

```bash
docker compose exec upload-portal python portalctl.py list --links
docker compose exec upload-portal python portalctl.py stats
docker compose exec upload-portal python portalctl.py rm <id>
docker compose exec upload-portal python portalctl.py sweep
```

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | the upload page |
| `GET` | `/api/config` | limits and lifetime, for the page |
| `POST` | `/api/upload/init` | reserve a slot, returns `uploadId` |
| `PUT` | `/api/upload/part?id=&i=` | one chunk, in order |
| `POST` | `/api/upload/done?id=` | finalise, returns the link |
| `GET` | `/s/{token}` | preview page with Open Graph tags |
| `GET` | `/s/{token}/meta` | what the viewer needs to pick a presentation |
| `GET` | `/s/{token}/file` | the file, with byte ranges |
| `GET` | `/s/{token}/preview` | browser-safe rendition of an image |
| `GET` | `/s/{token}/thumb` | small JPEG for the card and the poster |
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
