## Publisher:
- This is the module responsible for publishing data to the broker.
- it create as exchange: the component responsible for receiving data
    and publishing it to a queue.
- Publisher publishes data to an exchange, spicifing a routing key to identifie the message on the consumer's side.
- There are several publish methods associated with a consumer:
    - direct: This uses a message routing key, additional header set by the publisher and is used by the exchange to decide how to route the message
    - Topic: Uses a wildcard match between the routing key and the queue's binding routing pattern to decide how the messages will be routed. Messages can be routed to one or multiple queues depending on a pattern that matches a routing key. Routing key is  a list of words separated by a period

    - Fanout: This mode of exchange duplicates and directs messages to any queue regardless of routing keys and pattern matching. Commonly used when a message is meant to be shared with two or more queues.

    - Headers: 
    - Default Exchange: 
    - Dead letter Exchange:


### USER MANAGEMENT
- add new user:
    `rabbitmqctl add_user <username> <password>`
- make user admin:
    `rabbitmqctl set_user_tags <username> administrator`
- set permission for user:
    `rabbitmqctl set_permission -p <v_host> <username> ".*" ".*" ".*"`
    More on user permissions: https://www.rabbitmq.com/man/rabbitmqctl.1.man.html

