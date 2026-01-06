# An example of using standalone Python builds with multistage images.

# First, build the application in the `/app` directory
FROM ghcr.io/astral-sh/uv:bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Omit development dependencies
ENV UV_NO_DEV=1

# Configure the Python directory so it is consistent
ENV UV_PYTHON_INSTALL_DIR=/python

# Only use the managed Python version
ENV UV_PYTHON_PREFERENCE=only-managed

# Install Python before the project for caching
RUN uv python install 3.12

WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked

# Then, use a final image without uv
FROM debian:bookworm-slim AS final

# Install Playwright dependencies
# We use playwright install-deps but it needs the playwright cli.
# Since we don't have python/uv in the base image before copying, we can't easily run it.
# However, we can use the builder to generate a list or just install the known deps for chromium.
# Or we can install python/uv first, then install deps.
# Actually, the standard way is to use `playwright install --with-deps` but that needs root.

# Let's copy python env first so we can use playwright cli.
# Copy the Python version
COPY --from=builder --chown=python:python /python /python

# Copy the application from the builder
COPY --from=builder --chown=nonroot:nonroot /app /app

# Place executables in the environment at the front of the path
ENV PATH="/app/.venv/bin:$PATH"

# Install Playwright browsers and dependencies
# This needs to be done as root
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    # Install dependencies for chromium
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxcb1 \
    libxkbcommon0 \
    libx11-6 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

# Run playwright install to get the browser binaries
RUN playwright install chromium
# If dependencies are missing, 'playwright install --with-deps' would be better but it might try to install sudo.
# Since we are root, we can try 'playwright install-deps chromium' if the above manual list is insufficient.
RUN playwright install-deps chromium

# Setup a non-root user
RUN groupadd --system --gid 900 nonroot \
 && useradd --system --gid 900 --uid 900 --create-home nonroot

# Ensure nonroot can access the browsers
RUN chown -R nonroot:nonroot /ms-playwright

# Use the non-root user to run our application
USER nonroot

# Use `/app` as the working directory
WORKDIR /app

CMD ["python3", "-m", "hass_sgcc"]
