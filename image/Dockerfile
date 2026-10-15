# Stealthbox's image, built from the repository root by `stealthbox start`
# (docker-compose.yml). Everything comes from this repository except the browser
# core, which is prebuilt (browser/cloud) and downloaded from this repository's
# releases.
FROM python:3.12-slim

# Chromium's system libraries and fonts, the X display (Xvfb, openbox, xdotool),
# the viewer (x11vnc, noVNC) and tzdata for the container's timezone.
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdbus-1-3 libdrm2 libxkbcommon0 libatspi2.0-0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libx11-xcb1 libfontconfig1 libx11-6 \
    libxcb1 libxext6 libxshmfence1 \
    libglib2.0-0 libgtk-3-0 libpangocairo-1.0-0 libcairo-gobject2 \
    libgdk-pixbuf-2.0-0 libxss1 libxtst6 fonts-liberation \
    fonts-noto-color-emoji fonts-unifont fonts-freefont-ttf \
    fonts-ipafont-gothic fonts-wqy-zenhei fonts-tlwg-loma-otf \
    fonts-urw-base35 \
    xvfb xdotool openbox x11vnc novnc websockify tzdata xz-utils \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# cdp-server's launcher package: browser arguments, proxies, GeoIP.
COPY serve/pyproject.toml /opt/stealthbox/serve/pyproject.toml
COPY serve/stealthbox_launcher /opt/stealthbox/serve/stealthbox_launcher
RUN pip install --no-cache-dir /opt/stealthbox/serve

# Patchright: Playwright's API without the Runtime.enable call that some bot-check
# vendors detect. Under stock Playwright, such a page that has to issue a new
# token stays blank.
RUN pip install --no-cache-dir patchright==1.63.0
# A page closing mid-call would otherwise end Patchright's whole driver.
COPY image/patch-patchright.py /tmp/patch-patchright.py
RUN python3 /tmp/patch-patchright.py && rm /tmp/patch-patchright.py
# The agent's screenshots come from the X display (mss), scaled to CSS pixels (Pillow).
RUN pip install --no-cache-dir mss==10.2.0 pillow==12.3.0

# `stealthbox share`: a Cloudflare quick tunnel to the console, run by the chat API
# so a run that needs the user can hand them a link. The host needs nothing for it.
ARG TARGETARCH
RUN curl -fsSL --retry 3 -o /usr/local/bin/cloudflared \
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${TARGETARCH}" \
    && chmod +x /usr/local/bin/cloudflared \
    && cloudflared --version

# The browser: Fury's patched Chromium (browser/), built for Linux by
# browser/cloud/build-release.sh. cdp-server runs the pool, GeoIP and native
# input; STEALTHBOX_BINARY_PATH makes it launch launch-browser, which hands the
# core a persona config. It comes from this repository's release unless
# FURY_CORE_URL, when set, takes the tarballs from there instead (a local
# `python3 -m http.server` over browser/dist, for unreleased cores).
ARG FURY_CORE_REPO=jonclegg/stealthbox
ARG FURY_CORE_TAG=core-155.0.8059.12-linux.1
ARG FURY_CORE_URL=
ARG FURY_CORE_SHA256_AMD64=955a999a40e15d34f8637b3ded335bee934fd6c9485735b4bf1c8380ef739609
ARG FURY_CORE_SHA256_ARM64=9a12551d89d192ef53a4059bb0164c6be607c5c8dd92bfdcab61427d608fd706
RUN case "$TARGETARCH" in \
         amd64) arch=x64; sha="$FURY_CORE_SHA256_AMD64" ;; \
         arm64) arch=arm64; sha="$FURY_CORE_SHA256_ARM64" ;; \
       esac \
    && name="fury-core-linux-$arch.tar.xz" \
    && if [ -n "$FURY_CORE_URL" ]; then \
         curl -fsSL --retry 3 -o /tmp/fury-core.tar.xz "$FURY_CORE_URL/$name"; \
       else \
         curl -fsSL --retry 3 -o /tmp/fury-core.tar.xz "https://github.com/$FURY_CORE_REPO/releases/download/$FURY_CORE_TAG/$name"; \
       fi \
    && echo "$sha  /tmp/fury-core.tar.xz" | sha256sum -c - \
    && tar -xJf /tmp/fury-core.tar.xz -C /opt \
    && mv /opt/Fury /opt/fury \
    && rm /tmp/fury-core.tar.xz \
    && /opt/fury/chrome --version
COPY browser/personas/personas.json /opt/fury/personas.json
COPY browser/personas/mac-measured.json /opt/fury/mac-measured.json
COPY image/mac-fonts.conf /opt/stealthbox/mac-fonts.conf
COPY image/launch-browser /usr/local/bin/launch-browser
COPY serve/cdp-server /usr/local/bin/cdp-server
COPY serve/entrypoint.sh /entrypoint.sh
# STEALTHBOX_VERSION is what cdp-server gates its window settling on. Settling
# exists for older builds that report an incoherent window; with it on, Fury's
# Mac persona (a 33 px menu bar the X11 window cannot know about) fails every
# size and the launch is killed. launch-browser places the window on the
# persona's available area instead.
ENV STEALTHBOX_BINARY_PATH=/usr/local/bin/launch-browser \
    STEALTHBOX_VERSION=155.0.8059.12 \
    DISPLAY=:99

COPY image/index.html /usr/share/novnc/index.html
# Safari can keep a cached noVNC <=1.3 ui.js, which crashes on load if this
# element is missing (upstream removed it in 1.4). Newer ui.js ignores it. Inline
# display:none, because noVNC's CSS overrides the hidden attribute on inputs.
RUN sed -i 's#<textarea id="noVNC_clipboard_text" rows=5></textarea>#&\n                <input id="noVNC_clipboard_clear_button" type="button" style="display:none">#' /usr/share/novnc/vnc.html \
    && grep -q 'id="noVNC_clipboard_clear_button"' /usr/share/novnc/vnc.html
COPY image/stealthbox-serve.sh /usr/local/bin/stealthbox-serve
# `docker exec stealthbox stealthbox chat|key|smoke|share|status ...` works from any host
# shell (macOS, Linux, PowerShell), with nothing installed on the host.
COPY image/stealthbox-client.sh /usr/local/bin/stealthbox
RUN chmod +x /usr/local/bin/stealthbox-serve /usr/local/bin/stealthbox /usr/local/bin/launch-browser \
    /usr/local/bin/cdp-server /entrypoint.sh

# The chat API and its agent: Patchright (above) and the standard library.
COPY agent/*.py agent/console.html /opt/stealthbox/agent/
COPY agent/guidance /opt/stealthbox/agent/guidance
COPY examples/humanize.py examples/bing_first.py /opt/stealthbox/examples/
ENV STEALTHBOX_EXAMPLES=/opt/stealthbox/examples \
    STEALTHBOX_DATA=/data
LABEL org.opencontainers.image.source=https://github.com/jonclegg/stealthbox \
      org.opencontainers.image.description="Stealthbox: a stealth browser for AI agents" \
      org.opencontainers.image.licenses=MIT

EXPOSE 9222 6080 8423

ENTRYPOINT ["/entrypoint.sh"]
CMD ["stealthbox-serve"]
