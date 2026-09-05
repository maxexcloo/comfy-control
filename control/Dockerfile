FROM python:3.12.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /usr/local/bin/uv

ENV PATH=/opt/venv/bin:${PATH} \
    PORT=8000 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv

RUN uv venv "${VIRTUAL_ENV}"

WORKDIR /opt/comfy-control
COPY pyproject.toml uv.lock README.md ./
COPY catalogue ./catalogue
COPY control ./control
COPY providers ./providers
RUN uv export --frozen --no-dev --extra modal --no-emit-project \
      --no-hashes --output-file /tmp/requirements.txt \
    && uv pip install --python "${VIRTUAL_ENV}/bin/python" --requirements /tmp/requirements.txt \
    && rm /tmp/requirements.txt

ARG COMFY_CONTROL_REVISION
ENV COMFY_CONTROL_REVISION=${COMFY_CONTROL_REVISION}

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "control.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
