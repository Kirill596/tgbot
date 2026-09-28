FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd --create-home bot
USER bot
EXPOSE 8080
CMD ["sh", "-c", "alembic upgrade head && python -m app.main"]
