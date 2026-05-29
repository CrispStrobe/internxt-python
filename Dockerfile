FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Internxt credentials — set via docker run -e or docker-compose env_file
# Required:
#   INTERNXT_EMAIL        — your Internxt account email
#   INTERNXT_PASSWORD     — your Internxt account password
# Optional:
#   INTERNXT_TFA_SECRET   — base32 TOTP secret for auto-generating 2FA codes
#   WEBDAV_PORT           — port to expose (default: 3005)
#   WEBDAV_HOST           — bind address (default: 0.0.0.0 for container use)

ENV WEBDAV_PORT=3005
ENV WEBDAV_HOST=0.0.0.0

EXPOSE ${WEBDAV_PORT}

# Entrypoint: login then start WebDAV server
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

ENTRYPOINT ["/docker-entrypoint.sh"]
