# claude-hub-browser (v2 Phase 3 P3-6b): Google Chrome (pinned .deb, sha256-checked) + Xvfb + fluxbox + x11vnc/noVNC on
# the pinned python base (ops/hub/browser-pins.toml). Built on the server by the owner's browser paste
# (`docker compose build browser`), never pushed or pulled. No assistant code is baked in: browserloader runs the
# brain-built release (browser/run.py). Chrome's own apt source and cron job are removed: the pin is the only update
# path (a new pin = a new paste). Runs as the brain home's uid (BROWSER_UID, set by the paste), never root.
ARG BASE=missing
FROM ${BASE}
ARG CHROME_VERSION
ARG CHROME_SHA256
ARG BROWSER_UID=1000
ARG BROWSER_GID=1000
RUN set -eux; export DEBIAN_FRONTEND=noninteractive; \
    test -n "${CHROME_VERSION}" && test -n "${CHROME_SHA256}"; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl xvfb fluxbox x11vnc novnc websockify x11-apps \
      netpbm locales tzdata fonts-dejavu-core fonts-liberation fonts-noto-color-emoji libgl1-mesa-dri libegl1 \
      libegl-mesa0; \
    sed -i 's/^# *de_CH.UTF-8/de_CH.UTF-8/; s/^# *en_US.UTF-8/en_US.UTF-8/' /etc/locale.gen; locale-gen; \
    curl -fsSL --retry 3 --max-time 900 -o /tmp/chrome.deb \
      "https://dl.google.com/linux/chrome/deb/pool/main/g/google-chrome-stable/google-chrome-stable_${CHROME_VERSION}_amd64.deb"; \
    echo "${CHROME_SHA256}  /tmp/chrome.deb" | sha256sum -c -; \
    apt-get install -y --no-install-recommends /tmp/chrome.deb; rm -f /tmp/chrome.deb; \
    rm -f /etc/apt/sources.list.d/google-chrome.list /etc/apt/sources.list.d/google-chrome.sources \
      /etc/cron.daily/google-chrome; \
    apt-get purge -y curl; apt-get autoremove -y; rm -rf /var/lib/apt/lists/*; \
    getent group "${BROWSER_GID}" >/dev/null || groupadd -g "${BROWSER_GID}" browser; \
    getent passwd "${BROWSER_UID}" >/dev/null || useradd -u "${BROWSER_UID}" -g "${BROWSER_GID}" -M -d /tmp \
      -s /usr/sbin/nologin browser; \
    google-chrome-stable --version
ENV LANG=de_CH.UTF-8 HOME=/tmp
USER ${BROWSER_UID}:${BROWSER_GID}
