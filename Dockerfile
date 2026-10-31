# Use official Python runtime pinned by digest
FROM python:3.15.1-slim-bookworm@sha256:575f8bc12ec26542e0678bc6b60e385218248019d433157ec327264a78c3a853 AS builder

WORKDIR /build

# Build wheels in an isolated stage (botasaurus dependency is sourced from git).
# Digest-pinned Debian base; apt versions float with distro security updates.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /build/requirements.txt
RUN pip wheel --no-cache-dir --wheel-dir /build/wheels -r /build/requirements.txt

FROM python:3.15.1-slim-bookworm@sha256:575f8bc12ec26542e0678bc6b60e385218248019d433157ec327264a78c3a853

WORKDIR /app

# Install minimal browser/runtime dependencies for Botasaurus.
# Digest-pinned Debian base; apt versions float with distro security updates.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        chromium \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
COPY --from=builder /build/wheels /wheels
RUN grep -v '^botasaurus @ git+' /app/requirements.txt > /app/requirements.runtime.txt \
    && echo 'botasaurus' >> /app/requirements.runtime.txt \
    && pip install --no-cache-dir --no-index --find-links /wheels -r /app/requirements.runtime.txt \
    && python3 -c "import botasaurus.request" \
    && chmod -R a+rx "$(python3 -c 'import sysconfig; print(sysconfig.get_path("purelib"))')" \
    && rm -f /app/requirements.runtime.txt \
    && rm -rf /wheels

# Copy application code
COPY . /app

# Keep both paths available; Botasaurus integrations often look for google-chrome.
ENV CHROME_BIN=/usr/bin/chromium
RUN ln -sf /usr/bin/chromium /usr/bin/google-chrome

# Bake the publishing git SHA as the default Sentry release (overridable at runtime).
ARG GIT_SHA=unknown
ENV SENTRY_RELEASE=${GIT_SHA}

# Run as unprivileged user
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
# Named USER is enough inside this image; host UID mapping is not required.
# hadolint ignore=DL3066
USER appuser

EXPOSE 4010
# scrape_finished / scrape_rejected carry status, host and duration; access lines add no structure.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "4010", "--no-access-log"]
