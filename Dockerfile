FROM python:alpine3.7
RUN apk --no-cache add curl
COPY requirements.txt /requirements.txt
COPY . /app
WORKDIR /app
RUN pip install -r requirements.txt
EXPOSE 8085
ENTRYPOINT [ "python" ]
CMD [ "apiSharded.py", "run" ]