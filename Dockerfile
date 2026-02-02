#
# Copyright 2025 The Apache Software Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

# Multi-stage build for mail-mcp MCP server

# Stage 1: Builder - Install dependencies
FROM python:3.14-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc && \
    rm -rf /var/lib/apt/lists/*

# Copy dependency files and source code
COPY pyproject.toml README.adoc ./
COPY src/ ./src/

# Install the package and its dependencies using pip with PEP 621 support
RUN pip install --no-cache-dir --target=/install .

# Stage 2: Runtime - Minimal image
FROM python:3.14-slim

WORKDIR /app

# Install supercronic for scheduled tasks
# Latest release from https://github.com/aptible/supercronic/releases
# Multi-architecture support: amd64 and arm64
ARG TARGETARCH
ENV SUPERCRONIC_VERSION=v0.2.33 \
    SUPERCRONIC=/usr/local/bin/supercronic

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates && \
    SUPERCRONIC_ARCH=$(case "${TARGETARCH}" in \
        "amd64") echo "amd64" ;; \
        "arm64") echo "arm64" ;; \
        *) echo "amd64" ;; \
    esac) && \
    SUPERCRONIC_SHA1SUM=$(case "${TARGETARCH}" in \
        "amd64") echo "71b0d58cc53f6bd72cf2f293e09e294b79c666d8" ;; \
        "arm64") echo "e0f0c06ebc5627e43b25475711e694450489ab00" ;; \
        *) echo "71b0d58cc53f6bd72cf2f293e09e294b79c666d8" ;; \
    esac) && \
    curl -fsSLO "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${SUPERCRONIC_ARCH}" && \
    echo "${SUPERCRONIC_SHA1SUM}  supercronic-linux-${SUPERCRONIC_ARCH}" | sha1sum -c - && \
    chmod +x "supercronic-linux-${SUPERCRONIC_ARCH}" && \
    mv "supercronic-linux-${SUPERCRONIC_ARCH}" "$SUPERCRONIC" && \
    apt-get purge -y curl && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /install /usr/local/lib/python3.11/site-packages

# Copy CLI entry point scripts from builder
COPY --from=builder /install/bin/* /usr/local/bin/

# Copy application code
COPY src/ ./src/
COPY maven-jira-projects.toml ./

# Copy crontab for scheduler mode
COPY crontab /app/crontab

# Create directory for data
RUN mkdir -p /app/data

# Set Python path
ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1

# Environment variables for Elasticsearch connection
ENV MAIL_MCP_ELASTICSEARCH_URL=http://elasticsearch:9200
ENV MAIL_MCP_ELASTICSEARCH_INDEX_PREFIX=maven
ENV MAIL_MCP_LOG_LEVEL=INFO

# Create non-root user
RUN useradd -m -u 1000 mcp && \
    chown -R mcp:mcp /app

USER mcp

# Expose MCP server port for HTTP mode
EXPOSE 8080

# Run the MCP server
# Default to HTTP mode in Docker (can override with --transport stdio)
CMD ["python", "-m", "mail_mcp.server.main", "--transport", "http", "--host", "0.0.0.0", "--port", "8080"]
