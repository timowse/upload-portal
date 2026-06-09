# Upload Portal

GitHub Pages frontend for a clean on-demand upload link.

Important:
- GitHub Pages hosts the UI.
- The actual upload still needs a backend URL that the Pi provides when the portal is activated.
- Use config.json to switch between closed and active states.

Suggested production domain:
- upload.im-glauben.de

How to publish to GitHub Pages:
1. Push the repository.
2. Enable Pages on the main branch/root.
3. Set the custom domain if your DNS already points to GitHub Pages.

Activation flow:
- Start the Pi upload backend.
- Write the backend URL into config.json.
- Set active=true.
- Push config.json to the Pages repo.
- Send the Pages URL to the user.

Options supported by the frontend:
- single file or multi-file mode
- max upload count
- auto-close indicator
- clean iPhone-first layout
