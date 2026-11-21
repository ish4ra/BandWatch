# Test image: OpenWrt rootfs + a musl Python 3.13 copied from the official
# python:alpine image (the OpenWrt package mirror is not required).
# Only used by tests/openwrt/run.sh - never shipped.
ARG OPENWRT_TAG=x86-64-25.12.0
FROM python:3.13-alpine AS py
RUN mkdir -p /opt/py/bin /opt/py/lib && \
    cp /usr/local/bin/python3.13 /opt/py/bin/ && \
    cp -a /usr/local/lib/python3.13 /opt/py/lib/ && \
    rm -rf /opt/py/lib/python3.13/test /opt/py/lib/python3.13/idlelib /opt/py/lib/python3.13/tkinter /opt/py/lib/python3.13/site-packages/pip* && \
    for lib in $(for f in /usr/local/bin/python3.13 /usr/local/lib/python3.13/lib-dynload/*.so; do ldd $f 2>/dev/null; done | awk '/=>/ {print $3}' | sort -u); do \
      case "$lib" in /lib/ld-musl*|/lib/libc.musl*) ;; *) cp -L "$lib" /opt/py/lib/ ;; esac; done && \
    printf '#!/bin/sh\nLD_LIBRARY_PATH=/opt/py/lib exec /opt/py/bin/python3.13 "$@"\n' > /opt/py/python3 && chmod 755 /opt/py/python3

FROM openwrt/rootfs:${OPENWRT_TAG}
COPY --from=py /opt/py /opt/py
RUN cp /opt/py/python3 /usr/bin/python3 && mkdir -p /var/lock
