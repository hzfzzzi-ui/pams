FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY scheduler.py ./
COPY refresh_state.py ./
ENV TZ=UTC
CMD ["python", "-u", "scheduler.py"]
