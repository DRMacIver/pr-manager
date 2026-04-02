FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# ── System packages ──────────────────────────────────────────────────────────

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    clang \
    curl \
    git \
    jq \
    just \
    lld \
    locales \
    openssh-client \
    pkg-config \
    rsync \
    sudo \
    tmux \
    unzip \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Locale
RUN sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen && locale-gen
ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8

# ── GitHub CLI ───────────────────────────────────────────────────────────────

RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# ── Node.js LTS (needed by Claude Code) ─────────────────────────────────────

RUN curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# ── Non-root user ───────────────────────────────────────────────────────────

RUN useradd -m -s /bin/bash -G sudo dev \
    && echo "dev ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers.d/dev

USER dev
WORKDIR /home/dev
ENV HOME=/home/dev
ENV PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${HOME}/go/bin:/usr/local/go/bin:${PATH}"

# ── uv (Python package manager) ─────────────────────────────────────────────

RUN curl -LsSf https://astral.sh/uv/install.sh | sh

# ── Python (via uv) ─────────────────────────────────────────────────────────

RUN uv python install 3.13

# ── Rust ─────────────────────────────────────────────────────────────────────

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y \
      --default-toolchain stable \
      --profile default \
    && rustup toolchain install 1.86 \
    && rustup toolchain install nightly \
    && rustup component add rustfmt clippy llvm-tools-preview

# Cargo tools used in CI
RUN cargo install --locked cargo-llvm-cov cargo-docs-rs

# ── Go ───────────────────────────────────────────────────────────────────────

USER root
RUN ARCH=$(dpkg --print-architecture) \
    && curl -fsSL "https://go.dev/dl/$(curl -fsSL 'https://go.dev/VERSION?m=text' | head -1).linux-${ARCH}.tar.gz" \
       | tar -C /usr/local -xzf -
USER dev

# Go tools
RUN go install honnef.co/go/tools/cmd/staticcheck@latest

# ── Claude Code CLI ──────────────────────────────────────────────────────────

RUN npm install -g @anthropic-ai/claude-code

# ── Entrypoint setup ────────────────────────────────────────────────────────

COPY --chown=dev:dev container/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# gh wrapper that reads token from a mounted credentials file
COPY --chown=dev:dev container/gh-wrapper.sh /usr/local/bin/gh-auth-wrapper
RUN chmod +x /usr/local/bin/gh-auth-wrapper

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["bash"]
