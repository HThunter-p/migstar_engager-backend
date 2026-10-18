# Render demo deployment

This repository includes a Render Blueprint for a free, non-production demo. It creates a Django web service and a free PostgreSQL database.

## Deploy

1. Make the backend source available in its GitHub repository and open the Render dashboard.
2. Create a new **Blueprint** and select the backend repository. Render reads `render.yaml` from the repository root.
3. Set `CORS_ALLOWED_ORIGINS` to the exact origin of the hosted frontend, including `https://` and no path. For local-only testing, use the relevant local origin.
4. Apply the Blueprint and wait for the web service health check at `/api/settings/` to pass.
5. Use the service's `onrender.com` URL as the API host. The frontend must also be configured to use that URL instead of `http://127.0.0.1:8000/api`.
6. Create an administrator account before using the Django admin or the admin review tools.

Render generates `DJANGO_SECRET_KEY` for the service. Do not commit secrets or local environment files.

## Free-demo limitations

- Render's free web service has an ephemeral filesystem. Uploaded payment evidence stored under `media/` can be lost when the service restarts, spins down, or redeploys.
- The free PostgreSQL database expires 30 days after creation. It has a limited grace period to upgrade before Render deletes it and its data.
- The free web service may spin down when idle, so the first request can be slow.
- Use only disposable demo accounts and test data. Do not collect real payment evidence or rely on this setup to retain user data.

For a real launch, move to persistent database and file storage, review the production configuration, and update the frontend API URL and CORS origin.
