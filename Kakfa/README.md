# Kafka
Here's a basic deployment of Kafka using the Kafka and KafkaNodePool CRs from Strimzi operator, The goal I pretended here was to have a functional kafka cluster and integrate it with spark since kafka is very flexible in terms on high availability and security and strimzi is therefore one of the most sofisticated operators there are. While developping future project, I will try no emulate some of these capabilities in kafka.

I have also dug a bit on how Kafka Connect works and it is also completely deployable using strimzi for further integration with technologies such as NoSQL databases or S3-like storage environments through open connector deployable from the connectors hub on confluent webpage.

Finally there is also a debugging note where I discuss some empiric solutions that have proven to work out and possible explanation as to why they possibily do it.

