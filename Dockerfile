FROM python:3.12-slim
RUN apt-get update && apt-get install -y gcc && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PATH="/usr/local/bin:$PATH"
EXPOSE 8080
CMD ["python", "bot.py"]
