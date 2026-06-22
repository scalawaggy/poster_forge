FROM python:3.11-slim

WORKDIR /app

# Install OS-level dependencies (ffmpeg for yt-dlp merging)
# The rm -rf line clears the download cache to keep the container size small
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*
RUN apt-get update && \
    apt-get install -y ffmpeg nodejs && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*
# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir --upgrade yt-dlp
# Copy the entire modular app folder into the container
COPY poster_forge/ /app/

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]