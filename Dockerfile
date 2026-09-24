# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
FROM ghcr.io/astral-sh/uv:0.9.30-python3.11-bookworm-slim@sha256:4f5d923c9dcea037f57bda425dd209f3ec643da2f0b74227f68d09dab0b3bb36 AS nonroot-base

ENV DEBIAN_FRONTEND=noninteractive \
    UV_PYTHON_INSTALL_DIR=/opt/uv/python

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        git=1:2.39.5-0+deb12u3 \
        jq=1.6-2.1+deb12u2 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 validation \
    && useradd --uid 10001 --gid 10001 --create-home validation \
    && mkdir -p /opt/ci /opt/uv/python /opt/validation /workspace \
    && chown -R 10001:10001 \
        /home/validation /opt/ci /opt/uv/python /opt/validation /workspace

WORKDIR /opt/validation
COPY --chown=10001:10001 pyproject.toml uv.lock ./

USER 10001:10001

FROM nonroot-base AS tooling

USER root
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        shellcheck=0.9.0-1 \
    && rm -rf /var/lib/apt/lists/*
USER 10001:10001

ENV KF_RULE_CACHE_DIR=/home/validation/.cache/kingfisher/rule-cache

RUN --mount=type=secret,id=network-ca,target=/run/secrets/network-ca,uid=10001,required=false \
    if [ -f /run/secrets/network-ca ]; then \
        cat /etc/ssl/certs/ca-certificates.crt /run/secrets/network-ca \
            > /tmp/network-ca-bundle; \
        export UV_NATIVE_TLS=true SSL_CERT_FILE=/tmp/network-ca-bundle; \
    fi \
    && UV_PROJECT_ENVIRONMENT=/opt/ci/py311 \
        uv sync --locked --only-group tooling --python 3.11 \
        --no-managed-python --no-install-project \
    && /opt/ci/py311/bin/kingfisher rules compile-cache --no-update-check \
    && rm -f /tmp/network-ca-bundle \
    && rm -rf /home/validation/.cache/uv

RUN /opt/ci/py311/bin/python --version \
    && /opt/ci/py311/bin/ruff --version \
    && /opt/ci/py311/bin/yamllint --version \
    && /opt/ci/py311/bin/kingfisher --version \
    && uv --version \
    && git --version \
    && jq --version \
    && shellcheck --version

ENV PATH="/opt/ci/py311/bin:${PATH}" \
    UV_PROJECT_ENVIRONMENT=/opt/ci/py311

WORKDIR /workspace

FROM nonroot-base AS pytest-py311

RUN --mount=type=secret,id=network-ca,target=/run/secrets/network-ca,uid=10001,required=false \
    if [ -f /run/secrets/network-ca ]; then \
        cat /etc/ssl/certs/ca-certificates.crt /run/secrets/network-ca \
            > /tmp/network-ca-bundle; \
        export UV_NATIVE_TLS=true SSL_CERT_FILE=/tmp/network-ca-bundle; \
    fi \
    && UV_PROJECT_ENVIRONMENT=/opt/ci/py311 \
        uv sync --locked --no-dev --group test --group typing --python 3.11 \
        --no-managed-python --no-install-project \
    && rm -f /tmp/network-ca-bundle \
    && rm -rf /home/validation/.cache/uv

RUN /opt/ci/py311/bin/python --version \
    && /opt/ci/py311/bin/pytest --version \
    && /opt/ci/py311/bin/ty --version

ENV PATH="/opt/ci/py311/bin:${PATH}" \
    UV_PROJECT_ENVIRONMENT=/opt/ci/py311 \
    CI_PY311_ENVIRONMENT=/opt/ci/py311

WORKDIR /workspace

FROM nonroot-base AS pytest-py314

RUN --mount=type=secret,id=network-ca,target=/run/secrets/network-ca,uid=10001,required=false \
    if [ -f /run/secrets/network-ca ]; then \
        cat /etc/ssl/certs/ca-certificates.crt /run/secrets/network-ca \
            > /tmp/network-ca-bundle; \
        export UV_NATIVE_TLS=true SSL_CERT_FILE=/tmp/network-ca-bundle; \
    fi \
    && uv python install 3.14.3 --managed-python \
    && UV_PROJECT_ENVIRONMENT=/opt/ci/py314 \
        uv sync --locked --no-dev --group test --python 3.14.3 \
        --managed-python --no-install-project \
    && rm -f /tmp/network-ca-bundle \
    && rm -rf /home/validation/.cache/uv

RUN /opt/ci/py314/bin/python --version \
    && /opt/ci/py314/bin/pytest --version

ENV PATH="/opt/ci/py314/bin:${PATH}" \
    UV_PROJECT_ENVIRONMENT=/opt/ci/py314 \
    CI_PY314_ENVIRONMENT=/opt/ci/py314

WORKDIR /workspace
