FROM python:3.12-slim
WORKDIR /srv
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Playwright's bundled Node needs libatomic, which the slim Python image does not have
RUN apt-get update \
 && apt-get install -y --no-install-recommends libatomic1 \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# --only-shell: the robot always runs headless, so the small headless Chromium is enough
# (remove --only-shell if you ever want ERP_HEADLESS=false with a display)
RUN pip install --no-cache-dir -r requirements.txt \
 && playwright install --with-deps --only-shell chromium \
 && rm -rf /var/lib/apt/lists/*

COPY . .
EXPOSE 8000
CMD ["python", "-m", "app.main"]