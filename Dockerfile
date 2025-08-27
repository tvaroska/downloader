FROM python:3.11-slim

WORKDIR /app

RUN pip install uv

COPY . .

RUN uv pip install --system -e .[dev]

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "src.downloader.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]