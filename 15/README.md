# CRUD app with DB in helm-chart



## Helm-chart

Chart [helm-otus-15](helm-otus-15) contains:

- crud-15 app
- mariadb as subchart

### Deploy

`helm upgrade -i -n app  --create-namespace otus-15 helm-otus-15`

### API Schema

Swagger UI: http://arch.homework/docs

OpenAPI JSON: http://arch.homework/openapi.json



## Postman

[otus-15_postman.json](otus-15_postman.json)

### Run newman from docker

`docker run -v .:/app --add-host arch.homework=host-gateway postman/newman run /app/otus-15_postman.json`
