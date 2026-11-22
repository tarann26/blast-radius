# Same base image the organisers labelled with: its Python and SQLite decide which sqlglot tests run.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock
COPY tools/fetch_sqlglot.py /app/fetch_sqlglot.py
RUN python /app/fetch_sqlglot.py /app/repo && python -m compileall -q /app/repo/sqlglot /app/repo/tests; chmod -R a+rX /app/repo
COPY selector /app/selector
COPY map /app/map
RUN python -m compileall -q /app/selector && chmod -R a+rX /app
ENV BR_REPO=/app/repo BR_MAP=/app/map PYTHONDONTWRITEBYTECODE=1
CMD ["python", "/app/selector/main.py"]
