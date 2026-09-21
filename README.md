# Upload Portal

GitHub Pages frontend for a clean on-demand upload link.

Important:
- GitHub Pages hosts the UI.
- The actual upload still needs a backend URL that the Pi provides when the portal is activated.
- Use config.json to switch between closed and active states.

Production URL:
- https://upload.t1mo.dev/

DNS setup (domain registered at Strato):
- upload.t1mo.dev must be a CNAME to timowse.github.io.
- A subdomain created through Strato's panel points at Strato's own webspace
  (81.169.x.x) instead, which does not serve this site. Remove that subdomain
  hosting entry first, then add the CNAME record.
- A named Cloudflare Tunnel for the Pi backend requires the DNS zone to be
  hosted at Cloudflare, because its target (<uuid>.cfargotunnel.com) only
  resolves inside Cloudflare's DNS. Moving the nameservers to Cloudflare keeps
  the domain registered at Strato.

How to publish to GitHub Pages:
1. Push the repository.
2. Enable Pages on the main branch/root.
3. Point the DNS record at GitHub Pages, then set the custom domain in the Pages
   settings and wait for the certificate to be issued. The .dev TLD is
   HSTS-preloaded, so the site is only reachable once HTTPS works.

Activation flow:
- Start the Pi upload backend.
- Write the backend URL into config.json.
- Set active=true.
- Push config.json to the Pages repo.
- Send the Pages URL to the user.

Limits are enforced by the backend, not here:
- maxFiles, multiFile and autoClose only shape the UI. A direct POST to the
  backend URL bypasses all of them, so the Pi has to enforce them.

Options supported by the frontend:
- single file or multi-file mode
- max upload count
- auto-close indicator
- clean iPhone-first layout
