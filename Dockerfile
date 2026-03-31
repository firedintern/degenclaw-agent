FROM python:3.11-slim

# Install Node.js 20 and jq
RUN apt-get update && apt-get install -y curl jq && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install ta-lib
RUN apt-get update && apt-get install -y wget build-essential && \
    wget -q https://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz && \
    tar -xzf ta-lib-0.4.0-src.tar.gz && \
    cd ta-lib && ./configure --prefix=/usr && make && make install && \
    cd .. && rm -rf ta-lib ta-lib-0.4.0-src.tar.gz && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Node dependencies for ACP
COPY openclaw/openclaw-acp/package.json openclaw/openclaw-acp/package-lock.json* ./openclaw/openclaw-acp/
RUN cd openclaw/openclaw-acp && HUSKY=0 npm install --ignore-scripts

# Copy the rest of the app
COPY . .

CMD ["python", "-m", "src.bot"]
