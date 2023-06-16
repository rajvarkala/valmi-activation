FROM python:3.10

COPY requirements/test-requirements.txt /tmp/test-requirements.txt
RUN pip install --no-cache-dir --timeout 1000 -r /tmp/test-requirements.txt

RUN curl -fsSL https://get.docker.com | sh

WORKDIR /workspace 
ENV PYTHONUNBUFFERED 1

ENTRYPOINT ["/workspace/docker-entrypoint.sh"]
CMD ["uvicorn", "main:app",   "--port", "8000", "--host","0.0.0.0", "--log-level" ,"debug"]
