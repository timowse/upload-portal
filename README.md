# Upload Portal

A public file drop on a Raspberry Pi. Upload something, get a link, send it
on. Files delete themselves again.

```
share.t1mo.dev            the upload page
share.t1mo.dev/api/…      the API behind it        ─► Cloudflare Tunnel ─► Pi :8090
share.t1mo.dev/s/<token>  the preview page for one file
share.t1mo.dev/c/<token>  the gallery for several
```

One hostname for everything, served by the Pi. Page and API are same-origin,
so there is no CORS to configure and nothing to look up before the first
upload can start. GitHub Pages is not involved.

## No accounts, by choice

Anyone who opens either address can upload, and anyone holding a share link
can fetch that file until it expires. There is no login, because the point is
to hand a link to someone who should not have to make an account.

That also means the service is open to whoever finds it, and it will be
found: `share.t1mo.dev` appears in the public Certificate Transparency logs
the moment Cloudflare issues its certificate, and those logs get scanned. The
limits below are what stands between that and a full disk — they are
operational ceilings, not access control.

| Guard | Default | Setting |
| --- | --- | --- |
| Size per file | 5 GB, raisable to 10 | `PORTAL_BASE_GB`, `PORTAL_MAX_GB` |
| Uploads per address per hour | 30 | `PORTAL_UPLOADS_PER_HOUR` |
| Disk kept free | 10 GB | `PORTAL_KEEP_FREE_GB` |

An upload is refused with 507 once free space would fall below the floor, so
a full disk never takes the rest of the Pi down with it. A sweeper runs hourly
and also removes blobs no index entry claims.

## Bigger files, kept for less time

A file up to 5 GB is kept 30 days. The page offers a slider to raise the
ceiling a gigabyte at a time, up to 10 GB, and says what each step costs
before anything is sent:

| Up to | Kept |
| --- | --- |
| 5 GB | 30 days |
| 6 GB | 25 days |
| 7 GB | 20 days |
| 8 GB | 15 days |
| 9 GB | 10 days |
| 10 GB | 7 days |

The line runs straight from 30 days at 5 GB to 7 days at 10 GB. Its exact
values are figures like 25.4 and 16.2, so they are rounded to something worth
reading — fives while the number is large, whole days once it is small — and
both ends land on their stated value exactly. The figure follows the file's
real size rather than the slider, so a small file uploaded with the ceiling
raised still keeps the full 30 days. The preview page states how much longer the link
will work, so the person who receives it does not have to guess.

## One link, or one per file

Every upload gets its own link the moment it lands. Upload more than one file
and they also get a shared link, a gallery at `/c/<token>` where each tile
opens that file's own page. Both kinds work at once: hand out the bundle, or
hand out one file, or both.

A bundle holds no bytes of its own. It lives exactly as long as its
shortest-lived member, loses a file that was deleted or expired, and
disappears once nothing is left.

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
docker compose exec upload-portal python test_portal.py     # 54 checks
```

The Cloudflare Tunnel is dashboard-managed, so there is no `config.yml` on the
Pi. Its public hostname lives under Networks → Tunnels → Configure → published
application routes: `share` · `t1mo.dev` · HTTP · `localhost:8090`.

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
| `GET` | `/api/config` | limits and the size-to-days scale, for the page |
| `POST` | `/api/upload/init` | reserve a slot, returns `uploadId` |
| `PUT` | `/api/upload/part?id=&i=` | one chunk, in order |
| `POST` | `/api/upload/done?id=` | finalise, returns the link |
| `GET` | `/s/{token}` | preview page with Open Graph tags |
| `GET` | `/s/{token}/meta` | what the viewer needs to pick a presentation |
| `GET` | `/s/{token}/file` | the file, with byte ranges |
| `GET` | `/s/{token}/preview` | browser-safe rendition of an image |
| `GET` | `/s/{token}/thumb` | small JPEG for the card and the poster |
| `GET` | `/s/{token}/dl` | same file as an attachment |
| `POST` | `/api/bundle` | one link for several, from links already held |
| `GET` | `/c/{token}` | the gallery for a bundle |
| `GET` | `/c/{token}/meta` | its contents, for the page |
| `GET` | `/healthz` | container health check |

Thumbnails need Pillow, pillow-heif and ffmpeg, all of which the image
installs. If any of them fails, previews degrade to a download card rather
than breaking the page.

## DNS

One name, in the Cloudflare zone for `t1mo.dev`; the domain stays registered
at Strato.

| Name | Record | Proxy |
| --- | --- | --- |
| `share` | created by the tunnel | proxied |

`.dev` is HSTS-preloaded, so the name only works over HTTPS — there is no
plain HTTP fallback for debugging.

Note for anyone editing DNS here: creating a subdomain through Strato's panel
points it at Strato's own webspace rather than at the target, so a name that
has to reach something else needs an explicit record in Cloudflare.
