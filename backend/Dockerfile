FROM python:3.12-slim

# ffmpeg pulls the poster frame out of a video and is the fallback path for
# image formats Pillow was not built for.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/db.py backend/media.py backend/app.py backend/portalctl.py \
     backend/test_portal.py backend/viewer.html backend/bundle.html ./
# The same page GitHub Pages serves, so both hostnames work on their own.
COPY index.html ./

ENV PORTAL_DATA=/data
EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
