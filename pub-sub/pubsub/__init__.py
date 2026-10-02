"""Publish and consume through RabbitMQ without losing messages.

Modules:
    config: settings loaded and validated from environment variables.
    messages: the message envelope and its AMQP encoding.
    broker: connections, topology declaration and error classification.
    publisher: publishes with confirms (``python -m pubsub.publisher``).
    consumer: consumes with manual acks (``python -m pubsub.consumer``).
    demo: the end-to-end run behind ``make run``.
"""
