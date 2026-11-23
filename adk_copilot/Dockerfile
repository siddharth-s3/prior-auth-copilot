FROM python:3.12-slim

WORKDIR /app

# Optional TLS-intercepting-proxy TLS root(s): drop any *.pem/*.crt into certs/ (see README). With an
# empty certs/ folder this is a no-op, so a normal network needs nothing.
COPY certs/ /tmp/extra-certs/
RUN for f in /tmp/extra-certs/*.pem /tmp/extra-certs/*.crt; do [ -f "$f" ] && cat "$f" >> /etc/ssl/certs/ca-certificates.crt; done; true
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# AG-UI/A2UI adapter: installed as a second pass (resolving it together with google-adk[extensions] conflicts on google-genai).
RUN pip install --no-cache-dir ag-ui-adk==0.14.0

COPY agents ./agents
COPY agui_server.py .

EXPOSE 8000
CMD ["adk", "web", "--host", "0.0.0.0", "--port", "8000", "/app/agents"]
