FROM node:22-slim
WORKDIR /app
# Optional TLS-intercepting-proxy TLS root(s): drop *.pem/*.crt into certs/ (see README). Empty folder = no-op.
COPY certs/ /tmp/extra-certs/
RUN cat /tmp/extra-certs/*.pem /tmp/extra-certs/*.crt > /usr/local/share/extra-ca.pem 2>/dev/null; true
COPY package.json ./
RUN if [ -s /usr/local/share/extra-ca.pem ]; then export NODE_EXTRA_CA_CERTS=/usr/local/share/extra-ca.pem; fi; \
    npm install --no-audit --no-fund
COPY . .
ARG NEXT_PUBLIC_API=http://localhost:8002
ENV NEXT_PUBLIC_API=$NEXT_PUBLIC_API NEXT_TELEMETRY_DISABLED=1 NODE_OPTIONS=--max-old-space-size=6144
RUN npm run build
EXPOSE 3100
CMD ["npm", "run", "start"]
