FROM python:3.12-slim

WORKDIR /app

# Instala dependências
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia o job
COPY process_csv.py .

# Ponto de entrada
CMD ["python3", "process_csv.py"]
