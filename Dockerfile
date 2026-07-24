# Use official Python runtime pinned by digest
FROM python:3.14-slim-bookworm@sha256:4ff4b92a68355dbdb52584ab3391dff8d371a61d4e063468bfd0130e3189c6d9 AS builder

WORKDIR /build

# Build wheels in an isolated stage (botasaurus dependency is sourced from git).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /build/requirements.txt
RUN pip wheel --no-cache-dir --wheel-dir /build/wheels -r /build/requirements.txt

FROM python:3.14-slim-bookworm@sha256:4ff4b92a68355dbdb52584ab3391dff8d371a61d4e063468bfd0130e3189c6d9

WORKDIR /app

# Install minimal browser/runtime dependencies for Botasaurus.
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
    && rm -f /app/requirements.runtime.txt \
    && rm -rf /wheels

# Copy application code
COPY . /app

# Keep both paths available; Botasaurus integrations often look for google-chrome.
ENV CHROME_BIN=/usr/bin/chromium
RUN ln -sf /usr/bin/chromium /usr/bin/google-chrome

# Run as unprivileged user
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 4010
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "4010"]
