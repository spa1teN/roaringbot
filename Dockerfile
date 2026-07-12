FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY cogs/ cogs/
COPY core/ core/
COPY db/ db/
COPY scripts/ scripts/
COPY big.png big_square.png pb.png ./

EXPOSE 8080
CMD ["python", "bot.py"]
