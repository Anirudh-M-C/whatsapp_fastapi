FROM python:3.10-slim

WORKDIR /opt/odoo

RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    libxml2-dev \
    libxslt1-dev \
    libldap2-dev \
    libsasl2-dev \
    libjpeg-dev \
    libssl-dev \
    libffi-dev \
    libtiff-dev \
    libopenjp2-7-dev \
    libwebp-dev \
    libfreetype6-dev \
    zlib1g-dev \
    node-less \
    npm \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /var/lib/odoo

EXPOSE 8069

CMD ["python3", "odoo-bin", "--http-interface=0.0.0.0", "--http-port=8069"]