FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# configuration.ini is NOT baked into the image (see .dockerignore): mount it at
# /app/configuration.ini or pass settings as environment variables.
COPY . .

RUN mkdir -p /app/logs && chmod 777 /app/logs

EXPOSE 5001

# Create the local SQLite store on first start only (create_local_db.py recreates it).
CMD ["sh", "-c", "[ -f local_db.db ] || python create_local_db.py; exec gunicorn -w 4 --threads 4 -b 0.0.0.0:5001 eqdb:app"]
