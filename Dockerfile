FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    p7zip-full \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --root-user-action=ignore requests beautifulsoup4 rich flask

WORKDIR /app
COPY scripts/downloader.py /app/downloader.py
COPY scripts/xdcc_client.py /app/xdcc_client.py
COPY scripts/cli.py /app/cli.py
COPY scripts/app.py /app/app.py
COPY templates /app/templates
COPY static /app/static
COPY wishlist.md /app/wishlist.md

VOLUME ["/downloads"]

CMD ["python3", "/app/app.py"]
