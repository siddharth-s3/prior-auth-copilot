FROM python:3.12-slim

WORKDIR /app

# Optional TLS-intercepting-proxy TLS root(s): drop any *.pem/*.crt into certs/ (see README). With an
# empty certs/ folder this is a no-op, so a normal network needs nothing.
COPY certs/ /tmp/extra-certs/
RUN for f in /tmp/extra-certs/*.pem /tmp/extra-certs/*.crt; do [ -f "$f" ] && cat "$f" >> /etc/ssl/certs/ca-certificates.crt; done; true
# Python requests/huggingface_hub use certifi, not the system store — point them at the system bundle.
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

# Large wheels (PaddlePaddle/PaddleOCR) can stall on slow links; pip's default 15s read timeout is too short.
ENV PIP_DEFAULT_TIMEOUT=120
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ingest.py .

CMD ["python", "ingest.py", "--watch"]
