# Order + billing + notification app



## Architecture

* Microservices publish events to Kafka and consume them

* Each service has API for external calls

  * User:
    * Create User
    * Get user info

  * Order:
    * Create Order

  * Billing:
    * Create account
    * Get account info
    * Make Transaction

  * Notification:
    * Get list of notifications




![](Order%20architecture.drawio.png)

### Sequence diagram

![](Order%20sequence%20diagram.drawio.png)

## Helm-chart

Chart [helm-otus-26](helm-otus-26) contains:

- user - User management
- order - Order management
- billing - Account and Transaction management
- notification - Notifications
- Subcharts:
  - Kafka - 3-node Kafka cluster 
  - Mariadb


### Deploy

`helm upgrade -i -n app --create-namespace otus-26 helm-otus-26`

## Postman

[otus-26.postman_collection.json](otus-26.postman_collection.json)

How to run newman from docker:

`docker run --rm -v .:/app --add-host arch.homework=host-gateway postman/newman run /app/otus-26.postman_collection.json`
