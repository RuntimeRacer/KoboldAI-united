# RabbitMQ request processor for KoboldAI - (c) 2023 RuntimeRacer
# Test script - Simple script to test if stuff is working
import json

import pika

connection = pika.BlockingConnection(pika.ConnectionParameters(host='localhost', port=5672))
channel = connection.channel()

channel.queue_declare(queue='pygmalion_requests')

message_data = {
    "MessageID": "test-ID",
    "MessageBody": {
        "prompt": "Niko the kobold stalked carefully down the alley, his small scaly figure obscured by a dusky cloak that fluttered lightly in the cold winter breeze.",
        "temperature": 0.5,
        "top_p": 0.9
    }
}
message_json = json.dumps(message_data)

for i in range(5):
    channel.basic_publish(exchange='', routing_key='pygmalion_requests', body=message_json)
