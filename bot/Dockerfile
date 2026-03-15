FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir aiogram python-dotenv apscheduler

COPY bot.py .

CMD ["python3", "bot.py"]
