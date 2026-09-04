FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

RUN pip install --no-cache-dir playwright==1.62.0 starlette==1.6.0 uvicorn==0.52.4

WORKDIR /srv
COPY probe.py /srv/probe.py

USER pwuser
EXPOSE 8011
CMD ["uvicorn", "probe:app", "--host", "0.0.0.0", "--port", "8011", "--no-access-log"]
