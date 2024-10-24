# Final project



## Architecture

* Distributed transactions using two-phase commit pattern
  * Order - coordinator, Billing, Stock, Delivery - participants

* Idempotent API
  * Endpoint - /api/order/order

  * Idempotency-key - /api/order/get-key

* 





![](system%20architecture.drawio.png)

### Sequence diagram

![](Order%20sequence%20diagram.drawio.png)

## Helm-chart

Chart [helm-otus-30](helm-otus-30) contains:

- user - User management
- order - Order management
- billing - Account and Transaction management
- Stock - Stock inventory
- Delivery
- notification - Notifications
- Subcharts:
  - Kafka - 3-node Kafka cluster 
  - Mariadb
  - Prometheus


### Deploy

`helm upgrade -i -n app --create-namespace otus-30 helm-otus-30`

## Postman

[otus-30.postman_collection.json](otus-30.postman_collection.json)

How to run newman from docker:

`docker run --rm -v .:/app --add-host arch.homework=host-gateway postman/newman run /app/otus-30.postman_collection.json`
