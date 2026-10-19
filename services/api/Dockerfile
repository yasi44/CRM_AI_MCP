FROM python:3.12-slim AS build
WORKDIR /w
COPY requirements.txt .
# In your own repo pin exact versions (pip-compile / uv lock) for reproducible builds.
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN useradd --system --uid 10001 app
COPY --from=build /install /usr/local
WORKDIR /srv
COPY app ./app
USER 10001
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=5 CMD ["python","-m","app.cli","healthcheck"]
CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8080","--no-access-log"]
