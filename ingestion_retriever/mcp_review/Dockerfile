FROM python:3.12-slim

WORKDIR /app

# Optional TLS-intercepting-proxy TLS root(s): drop any *.pem/*.crt into certs/ (see README). With an
# empty certs/ folder this is a no-op, so a normal network needs nothing.
COPY certs/ /tmp/extra-certs/
RUN for f in /tmp/extra-certs/*.pem /tmp/extra-certs/*.crt; do [ -f "$f" ] && cat "$f" >> /etc/ssl/certs/ca-certificates.crt; done; true
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

COPY mcp_review/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mcp_review/mcp_review.py .

EXPOSE 8000
CMD ["python", "mcp_review.py"]
