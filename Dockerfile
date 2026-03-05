# syntax=docker/dockerfile:1.7

ARG TELEMT_REPO=https://github.com/telemt/telemt.git
ARG TELEMT_REF=main

FROM --platform=$TARGETPLATFORM dhi.io/alpine-base:3.23-dev AS build

ARG TELEMT_REPO
ARG TELEMT_REF

ENV RUSTUP_HOME="/usr/local/rustup" \
    CARGO_HOME="/usr/local/cargo" \
    PATH="/usr/local/cargo/bin:${PATH}"

RUN --mount=type=cache,target=/var/cache/apk \
    apk add --no-cache \
      ca-certificates git curl \
      build-base musl-dev pkgconf \
      openssl-dev openssl-libs-static \
      zlib-dev zlib-static \
    && update-ca-certificates

RUN set -eux; \
    case "$(apk --print-arch)" in \
      x86_64)  ARCH=x86_64  ;; \
      aarch64) ARCH=aarch64 ;; \
      *) echo "unsupported arch: $(apk --print-arch)"; exit 1 ;; \
    esac; \
    curl -fsSL "https://static.rust-lang.org/rustup/dist/${ARCH}-unknown-linux-musl/rustup-init" -o /tmp/rustup-init; \
    chmod +x /tmp/rustup-init; \
    /tmp/rustup-init -y --default-toolchain stable --profile minimal; \
    rm /tmp/rustup-init

ENV CARGO_NET_GIT_FETCH_WITH_CLI=true \
    CARGO_TERM_COLOR=always \
    CARGO_PROFILE_RELEASE_LTO=true \
    CARGO_PROFILE_RELEASE_CODEGEN_UNITS=1 \
    CARGO_PROFILE_RELEASE_DEBUG=false \
    CARGO_PROFILE_RELEASE_STRIP=true \
    CARGO_PROFILE_RELEASE_DEBUG_ASSERTIONS=false \
    CARGO_PROFILE_RELEASE_OVERFLOW_CHECKS=false \
    CARGO_PROFILE_RELEASE_PANIC=abort \
    OPENSSL_STATIC=1

WORKDIR /src

RUN --mount=type=cache,target=/root/.cache/git \
    git clone --depth=1 --branch "${TELEMT_REF}" "${TELEMT_REPO}" . \
    || (git init . && git remote add origin "${TELEMT_REPO}" \
        && git fetch --depth=1 origin "${TELEMT_REF}" \
        && git checkout --detach FETCH_HEAD)

RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    --mount=type=cache,target=/src/target \
    set -eux; \
    \
    cargo build --release --bin telemt; \
    \
    mkdir -p /out; \
    install -Dm755 target/release/telemt /out/telemt; \
    \
    if readelf -lW /out/telemt | grep -q "Requesting program interpreter"; then \
      echo "ERROR: telemt is dynamically linked -> cannot run in distroless/static"; \
      exit 1; \
    fi

FROM gcr.io/distroless/static:nonroot AS runtime

STOPSIGNAL SIGINT

COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=build /out/telemt /usr/local/bin/telemt

WORKDIR /tmp

EXPOSE 443/tcp 9090/tcp

USER nonroot:nonroot
ENTRYPOINT ["/usr/local/bin/telemt"]
CMD ["/etc/telemt.toml"]
