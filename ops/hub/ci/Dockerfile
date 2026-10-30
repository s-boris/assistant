# claude-hub-ci with the heavy media/ML tools (CI runner move, 29.10): the pinned python base plus what the last
# host-bound legacy suites need, so they run in the runner too. Built on the server (`docker compose build ci`,
# pull_policy: build), never pushed or pulled. No assistant code is baked in: hubloader runs the brain-built release.
#   apt (Debian trixie, --no-install-recommends): ffmpeg (trackid, tts, uwfix, trip-recap), tesseract-ocr + deu/eng/fra
#     + fonts-dejavu-core (vision), poppler-utils (letter: pdfinfo), rsync (proxy). Not gcc: test-all also needs the host-only
#     ~/opt/test-all (netguard source + entries), so it stays host-bound
#   /opt/ci-heavy/<venv>: the host's ~/opt venvs at the same pins on CPython 3.13 (the runner links <home>/opt/<venv>
#     to them): dive-venv (numpy, OpenCV: uwfix, trip-recap), trackinfo-venv (librosa), letter-venv (pypdf, pikepdf,
#     reportlab), piper-venv (piper-tts, onnxruntime: tts) + piper-voices (2 voices, sha256-checked)
#   numpy in the image's own Python (requirements/system.txt): recall's `#!/usr/bin/python3 -I` scripts ignore
#     PYTHONPATH, so the CI toolchain's site/ never reaches them; the brain's system Python has numpy too
#   every wheel hash-locked (pip --require-hashes, --only-binary: no install-time code), voices sha256-checked
# The runner stays network: none, read-only, cap_drop ALL, uid 10101: nothing here runs at start.
ARG BASE=missing
FROM ${BASE}
COPY requirements/ /opt/ci-heavy/requirements/
RUN set -eux; export DEBIAN_FRONTEND=noninteractive; \
    apt-get update; \
    apt-get install -y --no-install-recommends ffmpeg tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng \
      tesseract-ocr-fra fonts-dejavu-core poppler-utils rsync curl ca-certificates; \
    pip install --no-cache-dir --require-hashes --only-binary :all: -r /opt/ci-heavy/requirements/system.txt; \
    for v in dive trackinfo letter piper; do \
      python3 -m venv "/opt/ci-heavy/$v-venv"; \
      "/opt/ci-heavy/$v-venv/bin/pip" install --no-cache-dir --require-hashes --only-binary :all: \
        -r "/opt/ci-heavy/requirements/$v.txt"; \
    done; \
    d=/opt/ci-heavy/piper-voices; mkdir -p "$d"; \
    u=https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0; \
    while read -r sha path; do \
      curl -fsSL --retry 3 --max-time 600 -o "$d/${path##*/}" "$u/$path"; \
      echo "$sha  $d/${path##*/}" | sha256sum -c -; \
    done < /opt/ci-heavy/requirements/voices.sha256; \
    apt-get purge -y curl; apt-get autoremove -y; rm -rf /var/lib/apt/lists/* /root/.cache; \
    ffmpeg -hide_banner -version | head -1; tesseract --version 2>&1 | head -1; pdfinfo -v 2>&1 | head -1; \
    python3 -I -c "import numpy; print('numpy', numpy.__version__)"
