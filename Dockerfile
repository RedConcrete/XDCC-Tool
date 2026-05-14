FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    p7zip-full \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --root-user-action=ignore requests beautifulsoup4

WORKDIR /app
COPY scripts/downloader.py /app/downloader.py
COPY scripts/xdcc_client.py /app/xdcc_client.py
COPY wishlist.md /app/wishlist.md

VOLUME ["/downloads"]

CMD ["sleep", "infinity"]
