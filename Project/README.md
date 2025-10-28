# End-to-end Big Data analytics pipeline for MongoDB operational data 

Project Objective

The objective of this project is to learn how to integrate and use different Big Data technologies to create an end-to-end, production-like data pipeline inside my Kubernetes homelab.

Data Architecture

This will be the overall data architecture:

![Kubernetes architecture](Project.drawio.png)

By the end of the project, we will have an operational database hosted on MongoDB, whose changes are monitored using the Debezium connector for Change Data Capture (CDC) running in Kafka Connect.

Debezium will stream changes into corresponding Kafka topics in real time. Then, Spark will read data from Kafka and write it into the data lakehouse, which is built on MinIO (open-source S3 storage) and Nessie as the catalog.

The structure of the lakehouse follows the Medallion architecture pattern:

**- Bronze layer: Raw CDC data ingested from Kafka by Spark Streaming.**

**- Silver layer: Reconstructed database state derived from the bronze layer.**

**- Gold layer: Modeled and aggregated data built from the silver layer for analytics.**

⚠️ Note: Power BI can only visualize Hive tables directly. Therefore, the gold layer metadata will be built on top of Hive instead of Nessie + Iceberg. This enables us to use the Power BI Spark connector effectively.

Finally, a Spark Thrift Server will continuously serve data to Power BI through a DirectQuery connection, enabling near real-time analytics with an overall end-to-end data latency of approximately 2–3 minutes, from the moment data is stored in MongoDB to when it is displayed in Power BI visuals.



# Step 1: Data Generator Schema

The first step is to define the synthetic schema for the data generator:

Customers (customer_id, name, email, cell_phone, address, address_id, genre, age)

Suppliers (supplier_id, name, email, cell_phone, address_id)

Addresses (address_id, line_1, city, zip_code, state/region, country)

Orders(order_id, order_date, is_rebate, supplier_id or customer_id, address_id, product_id, product_price, product_quantity)

Products_orders (order_id, product_id, product_price, product_quantity)



# Step 2: MongoDB Deployment

Next, we deploy MongoDB inside Kubernetes.
The most efficient way to do this is by using the **MongoDB Controllers Kubernetes Operator** to deploy an instance of the MongoDBCommunity Custom Resource (CR).

This CR provides **high availability**, since each replica runs as a separate Kubernetes Pod. One Pod acts as the **primary instance**, and the others serve as **secondary replicas**, replicating the primary’s state.

The secondary replicas can be configured with either **strong** or **eventual consistency** relative to the primary instance. This is achieved by deploying and configuring the MongoDBCommunity CR as a **ReplicaSet** type.

**Data partitioning** can be achieved either by:

- Deploying a **Sharded Cluster** (requires a paid MongoDB subscription), or

- Deploying multiple ReplicaSets and managing sharding among them manually.

Given the size of my cluster (two Raspberry Pi 5s and a VM on my laptop), I chose to deploy a **single ReplicaSet** with two replicas.

This is the configuration used to deploy my MongoDBCommunity CR:

```yaml
# This example deploys a 3 members ReplicaSet with HostPath volumes
apiVersion: mongodbcommunity.mongodb.com/v1
kind: MongoDBCommunity
metadata:
  name: mongo
spec:
  members: 2 # We want two replicas
  security:
    authentication:
      modes:
      - SCRAM
  statefulSet:
    spec:
      template:
        spec:
          initContainers:
          - command: # This container simply runs a command that gives permissions that the pod to be able to make mongodb work.
              - chown
              - -R
              - "2000"
              - /data
            image: docker.io/library/busybox:1.36
            volumeMounts:
            - mountPath: /data # Must be the same as the command initContainers because it must exist inside the container for you to change its permissions.
              name: data-volume # This name must also be the data-volume name in volumeClaimTemplate since you are saying that the PV found by that PVC defined in volumeClaimTemplate is the one that will be mou>            securityContext:
              runAsNonRoot: false
              runAsUser: 0
              runAsGroup: 0
            name: change-dir-permissions
      volumeClaimTemplates: # We define the propertiesof the PVs we will ask for with PVCs
      - metadata: # First type of PVC for PV that host data
          name: data-volume
        spec:
          accessModes:
          - ReadWriteOnce
          resources:
            requests:
              storage: 10G
          selector:
            matchLabels: # Search for PVs with these labels
              type: data
      - metadata: # Second type for hosting the logs generated
          name: logs-volume
        spec:
          accessModes:
          - ReadWriteOnce
          resources:
            requests:
              storage: 5G
          selector:
            matchLabels: # Search for PVs with these labels
              type: logs
  type: ReplicaSet # We say that we want it to run as a ReplicaSet type, not as a ShardedCluster type
  users:
    - name: my-user # We create a user
      db: admin # It gets registered inside the admin collection
      passwordSecretRef: # A reference to the secret that will be used to generate the user's password
        name: my-user-password
      roles: # We give this user permissions, in this case, maximum permissions.
        - name: root
          db: admin
      scramCredentialsSecretName: my-scram # That's I think is the encryption used to store tha users data in admin
  version: 6.0.5 # That's the mongodb version we are using
```

We will also deploy the PVs and a Mongo Express a UI to be able to be able to interact in a simple and easy manner with Mongodb, and the data.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mongo-express
  namespace: mongodb
spec:
  replicas: 1
  selector:
    matchLabels:
      app: mongo-express
  template:
    metadata:
      labels:
        app: mongo-express
    spec:
      containers:
        - name: mongo-express
          image: mongo-express
          ports:
            - containerPort: 8081
          env:
            - name: ME_CONFIG_MONGODB_URL # This is how mongo-express connect mognodb, as you can see we put both replicas' services
              value: "mongodb://my-user:1234@mongo-0.mongo-svc.mongodb.svc.cluster.local:27017,mongo-1.mongo-svc.mongodb.svc.cluster.local:27017/?replicaSet=mongo"
            - name: ME_CONFIG_MONGODB_ENABLE_ADMI # That just gives the mongo-express UI permissions over the mongoDB
              value: "true"

---

apiVersion: v1
kind: Service # We create a service to access this mongo-express UI
metadata:
  name: mongo-express-svc
  namespace: mongodb
spec:
  selector:
    app: mongo-express
  ports:
    - protocol: TCP
      port: 8081
      targetPort: 8081
  type: NodePort # We make it a NodePort so we can use from outside the kubernetes cluster with my laptops' browser.
```
And for the PVs we have this configurations:

```yaml
# IMPORTANT: Even if we just made PVC types for logs ans data the operator creates 2 replicas with each this these types of PVC, so 4 PVs in total 2 for logs, 2 for data:
# THE REST OF THE PVS HAVE THE SAME EXACT CRITERIA AS THIS ONE
apiVersion: v1
kind: PersistentVolume
metadata:
  name: mongo-volume-0
  labels:
    type: data # This label will be used by the PVC to match the PVs
spec:
  accessModes:
  - ReadWriteOnce
  capacity:
    storage: 10G
  hostPath:
    path: /home/xavi/mongo/pv #This will be the hostpath in the node where the data from this PV will be stored
    type: ""
  nodeAffinity: # Here we define how the in which node the kubernetes API will create that PV
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/hostname
              operator: In
              values:
                - ubuntu # It will be created un my node called ubuntu
  persistentVolumeReclaimPolicy: Retain
  volumeMode: Filesystem # This property must be specified so that the PV uses the node file system.
---

apiVersion: v1
kind: PersistentVolume
metadata:
  labels:
    type: data
  name: mongo-volume-1
spec:
  accessModes:
  - ReadWriteOnce
  capacity:
    storage: 10G
  hostPath:
    path: /home/xavi/mongo/pv
    type: ""
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/hostname
              operator: In
              values:
                - ubuntu2
  persistentVolumeReclaimPolicy: Retain
  volumeMode: Filesystem

---

apiVersion: v1
kind: PersistentVolume
metadata:
  name: mongo-logs-volume-0
  labels:
    type: logs
spec:
  accessModes:
  - ReadWriteOnce
  capacity:
    storage: 5G
  hostPath:
    path: /home/xavi/mongo/logs
    type: ""
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/hostname
              operator: In
              values:
                - ubuntu
  persistentVolumeReclaimPolicy: Retain
  volumeMode: Filesystem

---

apiVersion: v1
kind: PersistentVolume
metadata:
  name: mongo-logs-volume-1
  labels:
    type: logs
spec:
  accessModes:
  - ReadWriteOnce
  capacity:
    storage: 5G
  hostPath:
    path: /home/xavi/mongo/logs
    type: ""
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/hostname
              operator: In
              values:
                - ubuntu2
  persistentVolumeReclaimPolicy: Retain
  volumeMode: Filesystem
```

Also, the secret used is this one:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: my-user-password
type: Opaque
stringData:
  password: "1234"
```


Lastly, the two nodeport that connect traffic from both mongodb replicas from outside the cluster are this ones:

```yaml
---
kind: Service
apiVersion: v1
metadata:
  name: external-mongo-service-0
  annotations:
    kube-linter.io/ignore-all: "used for sample"
spec:
  type: NodePort
  selector:
    app: mongo-svc
    statefulset.kubernetes.io/pod-name: mongo-0
  ports:
    - protocol: TCP
      nodePort: 31181
      port: 31181
      targetPort: 27017


---
kind: Service
apiVersion: v1
metadata:
  name: external-mongo-service-1
  annotations:
    kube-linter.io/ignore-all: "used for sample"
spec:
  type: NodePort
  selector:
    app: mongo-svc
    statefulset.kubernetes.io/pod-name: mongo-1
  ports:
    - nodePort: 31182
      port: 31182
      targetPort: 27017
```

I recommend following this order to deploy these yamls:
- 1. The **PVs** and the **secret**
- 2. Then we can deploy **mongodb** itself
- 3. Lastly, we can deploy **mongodb express** and the external **node ports**

After some minutes, you should be seeing something like that:

![Kubernetes architecture](mongo_running.png)

And we can also enter inside the browser to if Mongo Express works:


![Kubernetes architecture](mongo-express.png)


# Step 3: Data Ingestion

Once MongoDB is deployed, we use **Python** to connect to it and insert data.

The generator simulates a **continuous influx** of operational data flowing into MongoDB, which serves as our **operational database**.
The generator stops after 100 orders to avoid overloading the subsequent Spark jobs. It can be found in this folder under the name 
synthetic data generator.ipynb


# Step 4: Kafka, Kafka Connect, and Debezium Setup

Now it’s time to set up **Kafka, Kafka Connect**, and the **Debezium MongoDB connector**.

We use the **Strimzi Operator**, a popular and mature solution for deploying and managing Kafka ecosystems in Kubernetes.
It allows us to deploy **Kafka clusters, Kafka Connect clusters**, and **Kafka MirrorMaker**, as well as integrate **Kafka Cruise Control** for rebalancing of topics across brokers.

We’ll deploy **Kafka** using the following CRs and configurations:

```yaml
apiVersion: kafka.strimzi.io/v1beta2
kind: KafkaNodePool
metadata:
  name: dual-role
  namespace: kafka
  labels:
    strimzi.io/cluster: my-cluster # That's important, tells the kafkanodepools the name of the cluster they belong to
spec:
  replicas: 3 # Number of brokers
  roles:
    - controller # Since we don't use many brokers we can give them the controller role too so we don't have to make extra pods.
    - broker
  storage:
    type: jbod # Just a Bunch of Disks, option that when enabled allows a single broker to use various PVs, not really necessary
# since we set the storage to ephemeral.
    volumes:
      - id: 0
        type: ephemeral
        sizeLimit: 15Gi
        kraftMetadata: shared # Simplifies the deployment, the controller metadata is shared between broker and controller
# and no extra volume is needed for that.
---
apiVersion: kafka.strimzi.io/v1beta2
kind: Kafka
metadata:
  name: my-cluster
  namespace: kafka
  annotations:
    strimzi.io/node-pools: enabled # It has no search available KafkaNodePools CR to populate the Kafka cluster's brokers
    strimzi.io/kraft: enabled # It uses Kraft, a new method that substitutes Zookeeper and removes it
spec:
  kafka:
    version: 4.0.0 # Kafka version to use
    metadataVersion: 4.0-IV3
    listeners:
      - name: plain # First listener, the one which will we used, it works without TLS
        port: 9092
        type: internal # internal means the scope of the listener spans only within the cluster.
        tls: false
      - name: tls # The second one can be used to with TLS for message encryption between, I just activated it so you can ssee
        port: 9093 #to set it just by declaring it.
        type: internal
        tls: true
    config: # inside this part we define specific kafka configurations, the same we pass to manual kafka deployment without
 # strimzi as the operator between
      offsets.topic.replication.factor: 3
      transaction.state.log.replication.factor: 2
      transaction.state.log.min.isr: 2
      default.replication.factor: 3
      min.insync.replicas: 2
    metricsConfig: # We define metrics to be visible by prometheus and we define them...
      type: jmxPrometheusExporter
      valueFrom:
        configMapKeyRef:
          name: kafka-metrics # ...in the configmap with this name and...
          key: kafka-metrics-config.yml # ...using this part of the configmap
  entityOperator:
    topicOperator: {}
    userOperator: {}
  kafkaExporter:
    topicRegex: ".*"
    groupRegex: ".*"

---

kind: ConfigMap # This whole configmap is optional, not really necessary if you don't want to use it, the exact notation used
# is unkwown to me.
apiVersion: v1
metadata:
  name: kafka-metrics
  namespace: kafka
  labels:
    app: strimzi
data: # And this is the exact part of the configmap we use to state the metrics:
  kafka-metrics-config.yml: |
    # See https://github.com/prometheus/jmx_exporter for more info about JMX Prometheus Exporter metrics
    lowercaseOutputName: true
    rules:
    # Special cases and very specific rules
    - pattern: kafka.server<type=(.+), name=(.+), clientId=(.+), topic=(.+), partition=(.*)><>Value
      name: kafka_server_$1_$2
      type: GAUGE
      labels:
        clientId: "$3"
        topic: "$4"
        partition: "$5"
    - pattern: kafka.server<type=(.+), name=(.+), clientId=(.+), brokerHost=(.+), brokerPort=(.+)><>Value
      name: kafka_server_$1_$2
      type: GAUGE
      labels:
        clientId: "$3"
        broker: "$4:$5"
    - pattern: kafka.server<type=(.+), cipher=(.+), protocol=(.+), listener=(.+), networkProcessor=(.+)><>connections
      name: kafka_server_$1_connections_tls_info
      type: GAUGE
      labels:
        cipher: "$2"
        protocol: "$3"
        listener: "$4"
        networkProcessor: "$5"
    - pattern: kafka.server<type=(.+), clientSoftwareName=(.+), clientSoftwareVersion=(.+), listener=(.+), networkProcessor=(.+)><>      name: kafka_server_$1_connections_software
      type: GAUGE
      labels:
        clientSoftwareName: "$2"
        clientSoftwareVersion: "$3"
        listener: "$4"
        networkProcessor: "$5"
    - pattern: "kafka.server<type=(.+), listener=(.+), networkProcessor=(.+)><>(.+-total):"
      name: kafka_server_$1_$4
      type: COUNTER
      labels:
        listener: "$2"
        networkProcessor: "$3"
    - pattern: "kafka.server<type=(.+), listener=(.+), networkProcessor=(.+)><>(.+):"
      name: kafka_server_$1_$4
      type: GAUGE
      labels:
        listener: "$2"
        networkProcessor: "$3"
    - pattern: kafka.server<type=(.+), listener=(.+), networkProcessor=(.+)><>(.+-total)
      name: kafka_server_$1_$4
      type: COUNTER
      labels:
        listener: "$2"
        networkProcessor: "$3"
    - pattern: kafka.server<type=(.+), listener=(.+), networkProcessor=(.+)><>(.+)
      name: kafka_server_$1_$4
      type: GAUGE
      labels:
        listener: "$2"
        networkProcessor: "$3"
    # Some percent metrics use MeanRate attribute
    # Ex) kafka.server<type=(KafkaRequestHandlerPool), name=(RequestHandlerAvgIdlePercent)><>MeanRate
    - pattern: kafka.(\w+)<type=(.+), name=(.+)Percent\w*><>MeanRate
      name: kafka_$1_$2_$3_percent
      type: GAUGE
    # Generic gauges for percents
    - pattern: kafka.(\w+)<type=(.+), name=(.+)Percent\w*><>Value
      name: kafka_$1_$2_$3_percent
      type: GAUGE
    - pattern: kafka.(\w+)<type=(.+), name=(.+)Percent\w*, (.+)=(.+)><>Value
      name: kafka_$1_$2_$3_percent
      type: GAUGE
      labels:
        "$4": "$5"
    # Generic per-second counters with 0-2 key/value pairs
    - pattern: kafka.(\w+)<type=(.+), name=(.+)PerSec\w*, (.+)=(.+), (.+)=(.+)><>Count
      name: kafka_$1_$2_$3_total
      type: COUNTER
      labels:
        "$4": "$5"
        "$6": "$7"
    - pattern: kafka.(\w+)<type=(.+), name=(.+)PerSec\w*, (.+)=(.+)><>Count
      name: kafka_$1_$2_$3_total
      type: COUNTER
      labels:
        "$4": "$5"
    - pattern: kafka.(\w+)<type=(.+), name=(.+)PerSec\w*><>Count
      name: kafka_$1_$2_$3_total
      type: COUNTER
    # Generic gauges with 0-2 key/value pairs
    - pattern: kafka.(\w+)<type=(.+), name=(.+), (.+)=(.+), (.+)=(.+)><>Value
      name: kafka_$1_$2_$3
      type: GAUGE
      labels:
        "$4": "$5"
        "$6": "$7"
    - pattern: kafka.(\w+)<type=(.+), name=(.+), (.+)=(.+)><>Value
      name: kafka_$1_$2_$3
      type: GAUGE
      labels:
        "$4": "$5"
    - pattern: kafka.(\w+)<type=(.+), name=(.+)><>Value
      name: kafka_$1_$2_$3
      type: GAUGE
    # Emulate Prometheus 'Summary' metrics for the exported 'Histogram's.
    # Note that these are missing the '_sum' metric!
    - pattern: kafka.(\w+)<type=(.+), name=(.+), (.+)=(.+), (.+)=(.+)><>Count
      name: kafka_$1_$2_$3_count
      type: COUNTER
      labels:
        "$4": "$5"
        "$6": "$7"
    - pattern: kafka.(\w+)<type=(.+), name=(.+), (.+)=(.*), (.+)=(.+)><>(\d+)thPercentile
      name: kafka_$1_$2_$3
      type: GAUGE
      labels:
        "$4": "$5"
        "$6": "$7"
        quantile: "0.$8"
    - pattern: kafka.(\w+)<type=(.+), name=(.+), (.+)=(.+)><>Count
      name: kafka_$1_$2_$3_count
      type: COUNTER
      labels:
        "$4": "$5"
    - pattern: kafka.(\w+)<type=(.+), name=(.+), (.+)=(.*)><>(\d+)thPercentile
      name: kafka_$1_$2_$3
      type: GAUGE
      labels:
        "$4": "$5"
        quantile: "0.$6"
    - pattern: kafka.(\w+)<type=(.+), name=(.+)><>Count
      name: kafka_$1_$2_$3_count
      type: COUNTER
    - pattern: kafka.(\w+)<type=(.+), name=(.+)><>(\d+)thPercentile
      name: kafka_$1_$2_$3
      type: GAUGE
      labels:
        quantile: "0.$4"
    # KRaft overall related metrics
    # distinguish between always increasing COUNTER (total and max) and variable GAUGE (all others) metrics
    - pattern: "kafka.server<type=raft-metrics><>(.+-total|.+-max):"
      name: kafka_server_raftmetrics_$1
      type: COUNTER
    - pattern: "kafka.server<type=raft-metrics><>(current-state): (.+)"
      name: kafka_server_raftmetrics_$1
      value: 1
      type: UNTYPED
      labels:
        $1: "$2"
    - pattern: "kafka.server<type=raft-metrics><>(.+):"
      name: kafka_server_raftmetrics_$1
      type: GAUGE
    # KRaft "low level" channels related metrics
    # distinguish between always increasing COUNTER (total and max) and variable GAUGE (all others) metrics
    - pattern: "kafka.server<type=raft-channel-metrics><>(.+-total|.+-max):"
      name: kafka_server_raftchannelmetrics_$1
      type: COUNTER
    - pattern: "kafka.server<type=raft-channel-metrics><>(.+):"
      name: kafka_server_raftchannelmetrics_$1
      type: GAUGE
    # Broker metrics related to fetching metadata topic records in KRaft mode
    - pattern: "kafka.server<type=broker-metadata-metrics><>(.+):"
      name: kafka_server_brokermetadatametrics_$1
      type: GAUGE
```

And, as happened with MongoDB we can use **Kafdrop** as a UI to see summarized and general information about the state of the cluster and the data:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: kafdrop
  namespace: kafka
spec:
  replicas: 1
  selector:
    matchLabels:
      app: kafdrop
  template:
    metadata:
      labels:
        app: kafdrop
    spec:
      containers:
        - name: kafdrop
          image: obsidiandynamics/kafdrop
          ports:
            - containerPort: 9000
          env:
            - name: KAFKA_BROKERCONNECT
              value: "my-cluster-kafka-bootstrap.kafka:9092" # It has to connect to kafka using the bootstrpa servers.
---
apiVersion: v1
kind: Service
metadata:
  name: kafdrop
  namespace: kafka
spec:
  type: NodePort # And we make a NodePort so we can use our laptop browser to see it
  selector:
    app: kafdrop
  ports:
    - port: 9000
      targetPort: 9000
      nodePort: 30900
```
Let's look everything went as expected:

![Kubernetes architecture](kafka.png)

![Kubernetes architecture](kafka_.png)

And let's see of we can see anything from the broser with **Kafdrop**:

![Kubernetes architecture](kafdrop.png)

Now we can deploy **Kafka Connect**, to do it we will use this yaml:

Custom Image for Kafka Connect

A custom image is required because the Debezium connector is a **plugin** that must be placed as a **.jar** file inside the **Kafka Connect** container before it can be referenced via a **KafkaConnector CR**.

To build this image, we use the following **Dockerfile:**

```Dockerfile

FROM quay.io/strimzi/kafka:0.47.0-kafka-4.0.0

USER root

# CREATE DIRECTORY
RUN mkdir -p /opt/kafka/plugins

# COPY THE COMPPRESSED FILE, (I HAD THE DOWNLOADED JAR IN THE SAME DIRECTORY THE DOCKERFILE WAS IN SO THE DOCKER BUILD COMMAND TOOK IT FROM THERE)
COPY debezium-connector-mongodb-3.3.0-20251001.060017-332-plugin.tar.gz /tmp/

# DECOMPRESS AND PUT IT IN THE PLUGINS FOLDER
RUN tar -xzf /tmp/debezium-connector-mongodb-3.3.0-20251001.060017-332-plugin.tar.gz -C /opt/kafka/plugins/

# DELETE THE COMPRESSSED ORIGINAL FILE 
RUN rm /tmp/debezium-connector-mongodb-3.3.0-20251001.060017-332-plugin.tar.gz

# SET THE PERMISSION TO USE PLUGINS FOLDER IN CASE IT'S NOT ALLOWED
RUN chmod -R 755 /opt/kafka/plugins

```

Now use the **Docker Buildx**:

```sh
cd C:\Users\Usuario\Downloads\Kubernetes\Kakfa\connect docker buildx build --platform linux/amd64,linux/arm64 -t xavier418/kafka:connect-mongo-debezium --push .
```

It's important to say that inside my \connect folder I had the **Dockerfile** along with the donwloaded **.jar** so the command could access everything it need. Additionally, notice how I built it for both **amd64** (laptop) and **arm64** (Raspberry Pi) architectures using **Docker Buildx** and my **Docker hub repository** as the place to push it on.

With that finished, now we can create a **KafkaConnect CR**:

```yaml
apiVersion: kafka.strimzi.io/v1beta2
kind: KafkaConnect
metadata:
  name: my-clusterx-connect
  namespace: kafka
  annotations:
    strimzi.io/use-connector-resources: "true"
spec:
  version: "4.0.0" # Kafka version
  replicas: 1 # Only one réplica since the Debezium makes only one task
  bootstrapServers: "my-cluster-kafka-bootstrap.kafka:9092"
  image: docker.io/xavier418/kafka:connect-mongo-debezium-1.0 # We use my own image with the connector .jars incorporated
  config: # We tell the the Connect cluster to mimic these options from the kafka topics
    config.storage.replication.factor: -1
    offset.storage.replication.factor: -1
    status.storage.replication.factor: -1
# -1 means the default replication set on the broker
```

As allways we doble check the installation:

![Kubernetes architecture](kafka_connect.png)

And we finally proceed to create an instance of the connector with the following yaml:

```yaml
apiVersion: kafka.strimzi.io/v1beta2
kind: KafkaConnector
metadata:
  name: inventory-connector
  labels:
    strimzi.io/cluster: my-clusterx-connect
spec:
  class: io.debezium.connector.mongodb.MongoDbConnector
  tasksMax: 1
  config:
    mongodb.connection.string: "mongodb://my-user:1234@mongo-0.mongo-svc.mongodb.svc.cluster.local:27017,mongo-1.mongo-svc.mongodb.svc.cluster.local:27017/?replicaSet=mongo" # MongoDB URI
    topic.prefix: mongo
    database.include.list: "synthetic" #The mongodb database you will monitor for CDC
    capture.mode.full.update.type: "change_streams_update_full_with_pre-image" # Make the before field the populated when a delete occurs
    key.converter: "org.apache.kafka.connect.json.JsonConverter" # The format you want the topic keys to have
    value.converter: "org.apache.kafka.connect.json.JsonConverter" # The format you want the topic values to have
```
And doblecheck:

![Kubernetes architecture](connector.png)

You should now see on **KafDrop** the **topics** **synthetic.orders** and **synthetic.products** created automatically — each one corresponds to a MongoDB collection with the same name, as per the standard **Debezium** protocol:

![Kubernetes architecture](topics.png)

And if we enter inside them we will see how these topics contain all the info from the changes in the database, it's important to describe how Debezium works. By default, it extracts the **initial state** of the data and once it have it, it starts **monitoring the changes**, that will be important further on when we reconstruct the state of the data in Spark. 

Also, Kafka Connect can parallelize data production and consumption across Kafka topics, the MongoDB Debezium connector **cannot parallelize** CDC events when MongoDB is deployed as a **ReplicaSet**.
If you need to scale data ingestion, consider deploying MongoDB as a **Sharded Cluster**, as each shard can have its own Debezium connector instance.

# Disclaimer

Some security measures have been intentionally simplified or omitted.

This is because, while security is essential in production environments, implementing it here would add unnecessary complexity without much educational value for this project’s main goal: **integration.**

Security practices are **environment-specific**:

Kubernetes uses **RBAC rules**.

AWS uses **IAM roles** and **Security Groups**.

Power BI and Microsoft Fabric/Azure use **Microsoft Entra ID** within **Microsoft 365** environments.
 

