# The devcontainer should use the developer target and run as root with podman
# or docker with user namespaces.
FROM ghcr.io/diamondlightsource/ubuntu-devcontainer:resolute AS developer

# Add any system dependencies for the developer/build environment here
RUN apt-get update -y && apt-get install -y --no-install-recommends \
    graphviz \
    # Qt/PySide6 runtime libraries, needed to show the flash GUI over X11
    libegl1 \
    libgl1 \
    libglib2.0-0t64 \
    libxkbcommon-x11-0 \
    libdbus-1-3 \
    libfontconfig1 \
    libxcb-cursor0 \
    libxcb-icccm4 \
    libxcb-image0 \
    libxcb-keysyms1 \
    libxcb-randr0 \
    libxcb-render-util0 \
    libxcb-shape0 \
    libxcb-xinerama0 \
    libxcb-xkb1 \
    && apt-get dist-clean

# The build stage installs the context into the venv
FROM developer AS build

# Change the working directory to the `app` directory
# and copy in the project
WORKDIR /app
COPY . /app
RUN chmod o+wrX .

# Tell uv sync to install python in a known location so we can copy it out later
ENV UV_PYTHON_INSTALL_DIR=/python

# Sync the project without its dev dependencies
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-editable --no-dev --managed-python

# The runtime stage copies the built venv into a runtime container
FROM ubuntu:resolute AS runtime

# PySide6 includes Qt, but its Linux platform libraries come from the OS.
# This stage does not inherit the developer image's installed packages.
RUN apt-get update -y && apt-get install -y --no-install-recommends \
    ca-certificates \
    libegl1 \
    libgl1 \
    libglib2.0-0t64 \
    libxkbcommon-x11-0 \
    libdbus-1-3 \
    libfontconfig1 \
    libxcb-cursor0 \
    libxcb-icccm4 \
    libxcb-image0 \
    libxcb-keysyms1 \
    libxcb-randr0 \
    libxcb-render-util0 \
    libxcb-shape0 \
    libxcb-xinerama0 \
    libxcb-xkb1 \
    && rm -rf /var/lib/apt/lists/*

# Copy the python installation from the build stage
COPY --from=build /python /python

# Copy the environment, but not the source code
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH
# uv's managed Python may look for /etc/ssl/cert.pem; use Ubuntu's CA bundle.
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

# Exercise Qt and load the X11 plugin without requiring a display. Also check
# that HTTPS firmware downloads have a system trust store in this fresh image.
RUN QT_QPA_PLATFORM=offscreen python -c \
    "import ctypes, ssl; \
from PySide6.QtCore import QLibraryInfo; \
from PySide6.QtWidgets import QApplication; \
ctypes.CDLL(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath) + '/platforms/libqxcb.so'); \
app = QApplication([]); \
assert ssl.create_default_context().get_ca_certs(), 'Missing CA certificates'"

# change this entrypoint if it is not the same as the repo
ENTRYPOINT ["dls-d2bpm-tools"]
CMD ["--version"]
