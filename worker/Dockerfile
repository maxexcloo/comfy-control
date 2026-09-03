FROM python:3.14.7-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /usr/local/bin/uv

# renovate: datasource=git-tags packageName=https://github.com/Comfy-Org/ComfyUI.git
# ComfyUI v0.32.0. Keep the release label and immutable commit in sync.
ARG COMFYUI_REF=c2bcbecd82ec5ae66594340b395c24ef0217b238
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

RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential ca-certificates ffmpeg git && \
    rm -rf /var/lib/apt/lists/* && \
    uv venv "${VIRTUAL_ENV}" && \
    uv pip install --python "${VIRTUAL_ENV}/bin/python" pip

COPY worker/constraints.txt /tmp/worker-constraints.txt
RUN git init /opt/ComfyUI \
    && git -C /opt/ComfyUI remote add origin https://github.com/Comfy-Org/ComfyUI.git \
    && git -C /opt/ComfyUI fetch --depth 1 origin "${COMFYUI_REF}" \
    && git -C /opt/ComfyUI checkout --detach FETCH_HEAD \
    && uv pip install --python "${VIRTUAL_ENV}/bin/python" \
      --constraints /tmp/worker-constraints.txt \
      --requirements /opt/ComfyUI/requirements.txt \
    && rm /tmp/worker-constraints.txt

WORKDIR /opt/comfy-control
COPY pyproject.toml uv.lock README.md ./
COPY catalogue ./catalogue
COPY worker ./worker
RUN uv export --frozen --no-dev --extra build --extra vast --no-emit-project \
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
