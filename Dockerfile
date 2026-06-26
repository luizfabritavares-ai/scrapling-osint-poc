# Imagem oficial do Scrapling já vem com Python + browsers (Playwright/Chromium)
# instalados. Em cima dela só adicionamos o FastAPI/uvicorn e o app.
FROM pyd4vinci/scrapling:latest

WORKDIR /app

# Garante FastAPI/uvicorn (scrapling[fetchers] já está na imagem base)
RUN pip install --no-cache-dir "fastapi>=0.115,<1.0" "uvicorn[standard]>=0.30,<1.0" "pydantic>=2.5,<3.0"

COPY main.py .

# Railway injeta a porta em $PORT
ENV PORT=8080
EXPOSE 8080

# shell-form pra expandir $PORT
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT}
