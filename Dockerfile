FROM python:3.12-slim

WORKDIR /app

RUN useradd --create-home --uid 1000 tayder
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
RUN mkdir -p /app/data && chown -R tayder:tayder /app

USER tayder
ENV JOURNAL_DB_PATH=/app/data/tayder.db
ENV MODE=paper
ENV BANKROLL_USD=10

CMD ["python", "-m", "tayder"]
