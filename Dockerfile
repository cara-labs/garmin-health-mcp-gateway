FROM python:3.12.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --gid 10001 gateway \
    && useradd --uid 10001 --gid gateway --create-home --shell /usr/sbin/nologin gateway

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY migrations ./migrations
RUN python -m pip install --upgrade pip \
    && python -m pip install .

USER 10001:10001
ENTRYPOINT ["garmin-health"]
CMD ["scheduler"]
