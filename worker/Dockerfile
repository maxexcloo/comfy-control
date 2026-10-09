FROM python:3.14.8-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.12.24 /uv /usr/local/bin/uv

# renovate: datasource=git-tags packageName=https://github.com/Comfy-Org/ComfyUI.git
# ComfyUI v0.39.2. Keep the release label and immutable commit in sync.
ARG COMFYUI_REF=3c1b7a17fdf239d35ce98cc777756f572d089795
ARG MODEL_PROFILE
ENV BUILTIN_CATALOGUE_DIR=/opt/comfy-control/catalogue \
    COMFYUI_DIR=/opt/ComfyUI \
    COMFYUI_URL=http://127.0.0.1:8188 \
    DEBIAN_FRONTEND=noninteractive \
    HF_XET_HIGH_PERFORMANCE=1 \
    MODEL_PROFILES=flux-2-klein-9b \
    WORKER_MODE=pod \
    PATH=/opt/venv/bin:${PATH} \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv

RUN sed -i "s|http://deb.debian.org|https://deb.debian.org|g" /etc/apt/sources.list.d/debian.sources && \
    apt-get update && \
    apt-get install --no-install-recommends -y build-essential ca-certificates ffmpeg git && \
    rm -rf /var/lib/apt/lists/* && \
    uv venv "${VIRTUAL_ENV}" && \
    uv pip install --python "${VIRTUAL_ENV}/bin/python" pip

COPY worker/constraints.txt /tmp/worker-constraints.txt
RUN git init /opt/ComfyUI \
    && git -C /opt/ComfyUI remote add origin https://github.com/Comfy-Org/ComfyUI.git \
    && git -C /opt/ComfyUI fetch --depth 1 origin "${COMFYUI_REF}" \
    && git -C /opt/ComfyUI checkout --detach FETCH_HEAD \
    && uv pip install --constraints /tmp/worker-constraints.txt \
      --python "${VIRTUAL_ENV}/bin/python" \
      --requirements /opt/ComfyUI/requirements.txt \
    && rm /tmp/worker-constraints.txt

WORKDIR /opt/comfy-control
COPY pyproject.toml uv.lock README.md ./
COPY catalogue ./catalogue
COPY worker ./worker
RUN uv export --extra build --extra vast --frozen --no-dev --no-emit-project \
      --no-hashes --output-file /tmp/requirements.txt \
    && uv pip install --python "${VIRTUAL_ENV}/bin/python" --requirements /tmp/requirements.txt \
    && rm /tmp/requirements.txt

RUN --mount=type=secret,id=HF_TOKEN,env=HF_TOKEN \
    --mount=type=secret,id=CIVITAI_TOKEN,env=CIVITAI_TOKEN \
    if [ -n "${MODEL_PROFILE}" ]; then \
      python -c 'import os; from pathlib import Path; from catalogue.fetch import fetch_profile; fetch_profile(Path("/opt/comfy-control/catalogue/profiles") / (os.environ["MODEL_PROFILE"] + ".yaml"), Path("/opt/ComfyUI/models"))'; \
    fi

ARG COMFY_CONTROL_REVISION
ENV COMFY_CONTROL_REVISION=${COMFY_CONTROL_REVISION}

EXPOSE 8000
CMD ["python", "-m", "worker.supervisor"]
