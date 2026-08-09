FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# DejaVu Sans Bold — used by _make_tba_placeholder() for readable TBA event covers
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core && rm -rf /var/lib/apt/lists/*

COPY bot.py .
COPY cogs/ cogs/
COPY core/ core/
COPY db/ db/
COPY scripts/ scripts/
COPY resources/big.png resources/big_square.png resources/pb.png resources/

EXPOSE 8080
CMD ["python", "bot.py"]
