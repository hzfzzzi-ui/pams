FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY scheduler.py ./
COPY refresh_state.py ./
COPY flex_confirmation.py ./
COPY imap_bill_watcher.py ./
ENV TZ=UTC
CMD ["python", "-u", "scheduler.py"]
