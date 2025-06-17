# ai-agent/Dockerfile
FROM python:3.10-slim

# 1. Install git (if you ever use GitPython here) and curl (optional)
RUN apt-get update && \
    apt-get install -y git && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 2. Copy and install dependencies, including python-dotenv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt python-dotenv

# 3. Copy your AI-agent code
COPY . .

# 4. Ensure OpenAI key is loaded from .env
ENV OPENAI_API_KEY=${OPENAI_API_KEY}

# 5. Run the server
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8200"]
