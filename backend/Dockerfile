FROM python:3.12-slim

# ffmpeg pulls the poster frame out of a video and is the fallback path for
# image formats Pillow was not built for.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY store.py shares.py auth.py app.py portalctl.py test_portal.py viewer.html admin.html ./

ENV PORTAL_DATA=/data
EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
