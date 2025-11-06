# A Turning MongoDB Ops Data into Near Real-Time Power BI Insights on Kubernetes

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

# Step 5: Setting Up the Data Lakehouse Ecosystem

Now we will proceed to set up the data lakehouse ecosystem.

# Deploying MinIO

The first component to deploy is **MinIO.**
To do that, we will use the **MinIO Operator** (note: this is not the AIStor Operator, which is a paid product offering extra enterprise features on top of MinIO).

📘 **Reference** to MinIO Operator GitHub Repository (https://github.com/minio/operator)

Once the operator is installed, we can deploy a **Tenant CR** to create a **MinIO** tenant:

**Tenant CR:**
```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: minio-tenant
---
apiVersion: v1
kind: Secret
metadata:
  name: storage-configuration
  namespace: minio-tenant
stringData:
  config.env: |-
    export MINIO_ROOT_USER="minio"
    export MINIO_ROOT_PASSWORD="minio123"
    export MINIO_STORAGE_CLASS_STANDARD="EC:2"
    export MINIO_BROWSER="on"
type: Opaque
---
apiVersion: v1
data:
  CONSOLE_ACCESS_KEY: Y29uc29sZQ==
  CONSOLE_SECRET_KEY: Y29uc29sZTEyMw==
kind: Secret
metadata:
  name: storage-user
  namespace: minio-tenant
type: Opaque
---
apiVersion: minio.min.io/v2
kind: Tenant
metadata:
  annotations: # Metrics for prometheus, in this porject they are not used
    prometheus.io/path: /minio/v2/metrics/cluster
    prometheus.io/port: "9000"
    prometheus.io/scrape: "true"
  labels:
    app: minio
  name: myminio
  namespace: minio-tenant
spec:
  certConfig: {}
  configuration:
    name: storage-configuration
  env: []
  requestAutoCert: false
  features:
    bucketDNS: false
    domains: {}
  image: quay.io/minio/minio:RELEASE.2025-04-08T15-41-24Z
  imagePullSecret: {}
  mountPath: /export
  podManagementPolicy: Parallel
  pools:
  - affinity:
      nodeAffinity: {}
      podAffinity: {}
      podAntiAffinity: {}
    containerSecurityContext:
      allowPrivilegeEscalation: false
      capabilities:
        drop:
        - ALL
      runAsGroup: 1000
      runAsNonRoot: true
      runAsUser: 1000
      seccompProfile:
        type: RuntimeDefault
    name: pool-0 # pool-0 is a set of servers, each server is different pod. 
    nodeSelector: {}
    resources: {}
    securityContext:
      fsGroup: 1000
      fsGroupChangePolicy: OnRootMismatch
      runAsGroup: 1000
      runAsNonRoot: true
      runAsUser: 1000
    servers: 4 # Each server can have a different number of volumes 
    tolerations: []
    topologySpreadConstraints: []
    volumeClaimTemplate: # This is the definition of PVCs, one of these for each volume
      apiVersion: v1
      kind: persistentvolumeclaims
      metadata: {}
      spec:
        accessModes:
        - ReadWriteOnce
        resources:
          requests:
            storage: 10Gi
        storageClassName: local-storage # That will link existing PVs with that storage class
      status: {}
    volumesPerServer: 1 # In this case, we have 4 servers, each with a single volume.
  priorityClassName: ""
  serviceAccountName: ""
  serviceMetadata:
    consoleServiceAnnotations: {}
    consoleServiceLabels: {}
    minioServiceAnnotations: {}
    minioServiceLabels: {}
  subPath: ""
  users:
  - name: storage-user
```
In my case, I have used **static PersistentVolumes (PVs)** with **hostPaths**, where each PV uses a directory on the host node as storage.

Tenants are composed of pools, which in turn are composed of **servers**, and each server can have multiple **volumes** — each corresponding to a separate PV.
If you use hostPath-based PVs as I did, take into account that **all volumes belonging to a given server must be located on the same node**. This ensures that the Pod created for that server can access all PVs, since they reside on the same node.

Of course, for the Tenants to run properly, their **PVCs** need to match with some **PVs** so let's create them:

```yaml
---
apiVersion: v1 # All PVs follow the exact same logic
kind: PersistentVolume
metadata:
  name: minio-pv-1
spec:
  capacity:
    storage: 10Gi
  volumeMode: Filesystem # Must be this type to use a node's directory
  accessModes:
    - ReadWriteOnce
  persistentVolumeReclaimPolicy: Retain
  storageClassName: local-storage # We use the local-storage class to relate PVC with the PVs
  local:
    path: /home/xavi/minio/pv1 # The path inside the node
  nodeAffinity: # With that affinity we force them to be all inside ubuntu2 node
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/hostname
              operator: In
              values:
                - ubuntu2
---

apiVersion: v1
kind: PersistentVolume
metadata:
  name: minio-pv-2
spec:
  capacity:
    storage: 10Gi
  volumeMode: Filesystem
  accessModes:
    - ReadWriteOnce
  persistentVolumeReclaimPolicy: Retain
  storageClassName: local-storage
  local:
    path: /home/xavi/minio/pv2
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/hostname
              operator: In
              values:
                - ubuntu2

---

apiVersion: v1
kind: PersistentVolume
metadata:
  name: minio-pv-3
spec:
  capacity:
    storage: 10Gi
  volumeMode: Filesystem
  accessModes:
    - ReadWriteOnce
  persistentVolumeReclaimPolicy: Retain
  storageClassName: local-storage
  local:
    path: /home/xavi/minio/pv3
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/hostname
              operator: In
              values:
                - ubuntu2

---

apiVersion: v1
kind: PersistentVolume
metadata:
  name: minio-pv-4
spec:
  capacity:
    storage: 10Gi
  volumeMode: Filesystem
  accessModes:
    - ReadWriteOnce
  persistentVolumeReclaimPolicy: Retain
  storageClassName: local-storage
  local:
    path: /home/xavi/minio/pv4
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/hostname
              operator: In
              values:
                - ubuntu2
```
In my case, I have used static PersistentVolumes (PVs) with hostPaths, where each PV uses a directory on the host node as storage.

Tenants are composed of servers, and each server can have multiple volumes — each corresponding to a separate PV.
If you use hostPath-based PVs as I did, take into account that all volumes belonging to a given server must be located on the same node. This ensures that the Pod created for that server can access all PVs, since they reside on the same node.

Once we have everyting well set, the kubectl should return us this output:

![Kubernetes architecture](minio.png)

We also see how the S3 UI works, to that I use the command:

```bash
kubectl edit svc myminio-console -n minio-tenant
```
And inside it I utrn it into a NodePort like this:

```yaml
apiVersion: v1
kind: Service
metadata:
  creationTimestamp: "2025-10-17T10:20:55Z"
  name: myminio-console
  namespace: minio-tenant
  ownerReferences:
  - apiVersion: minio.min.io/v2
    blockOwnerDeletion: true
    controller: true
    kind: Tenant
    name: myminio
    uid: 6bb8c82a-4f41-4e4d-90c4-e09421cfa817
  resourceVersion: "1994012"
  uid: 366620d0-6a2b-4464-9318-e3bd20749dae
spec:
  clusterIP: 10.111.56.80
  clusterIPs:
  - 10.111.56.80
  externalTrafficPolicy: Cluster
  internalTrafficPolicy: Cluster
  ipFamilies:
  - IPv4
  ipFamilyPolicy: SingleStack
  ports:
  - name: http-console
    nodePort: 31511
    port: 9090
    protocol: TCP
    targetPort: 9090
  selector:
    v1.min.io/tenant: myminio
  sessionAffinity: None
  type: NodePort
status:
  loadBalancer: {}
```

💡Note: Don't worry about the operator, it will not undo this action while reconciling so this change is persistent.

Now we can enter the console and see how minio works from the ease of the UI:

Inside the **UI** we can see the objects inside our buckets with the **object browser**:

![Kubernetes architecture](minio-console.png)

We can also create new **keys** (the ones we have seen before) that inherit the current user permissions:

![Kubernetes architecture](minio-keys.png)

And finally why can manage **buckets** from here:

![Kubernetes architecture](minio-buckets.png)



# Deploying Nessie

Next, we’ll deploy **Project Nessie** using its **Helm chart**.
You can find deployment details in the official documentation. It works like any other Helm deployment.

Here is the **values.yaml** configuration file used for the helm install command:

```yaml
versionStoreType: MONGODB2 # We will leverage the fact that Nessie is a modern framework that also allows the use of MongoDB as it's metastore with a database in mongo called nessie to host tables metadata.
mongodb:
  database: nessie # It has to be created in using mongo-express first.
  connectionString: "mongodb://mongo-0.mongo-svc.mongodb.svc.cluster.local:27017,mongo-1.mongo-svc.mongodb.svc.cluster.local:27017/?replicaSet=mongo"
  secret: # The secret contains the mongodb credentials so that nessie can read and write in nessie database
    name: mongodb-creds
    username: mongodb_username
    password: mongodb_password
catalog:
  iceberg: # The type of catalog is Iceberg
    defaultWarehouse: WH
    WH:
      location: s3a://synthetic # S3 location for the bucket
  storage:
    s3:
      defaultOptions:
        endpoint: http://myminio-hl.minio-tenant.svc.cluster.local:9000 # myminio-hl service DNS address
        pathStyleAccess: true # In this mode the endpoint doesn't contains the bucket
        accessKeySecret: # The keys for accessing the MinIO (they can be overrriden the Spark-to-S3 confs "spark.hadoop.fs.s3a.keys" on the Spark session)
          awsAccessKeyId: wGyHqcx43ZGp86XtqWm8
          awsSecretAccessKey: 89DlA5N6PfNE8o0XptU9zby6niweY2mXZkNbFsl4
          name: null
```

The keys for **accessing MinIO** can be **overrriden** by the Spark-to-S3 confs **"spark.hadoop.fs.s3a.keys"** on the **PySpark** session.

In my setup, storage is backed by **MongoDB as metastore**.

💡 Note: If the Nessie Pod fails and metadata is stored ephemerally (inside the Pod), the next Nessie Pod will **not** be able to retrieve previously created tables, even if the table data persists in S3. That' why we have to use a metadata store as well as a data warehouse on s3. 

Once again we change the service to be a Node Port and we can access the **Nessie UI**, for now there are no tables but we will be able to see them once we create them with the nessie catalog:

![Kubernetes architecture](nessie.png)

# Configuring Spark

Now we’ll configure **Spark session** — not only to perform the transformations from Kafka topics, but also to act as the **main networking layer** between **Nessie** and **MinIO**, coordinating them to form a functional **data lakehouse** using the **Iceberg** table format.

For this setup, I found it very convenient to use **Spark-on-Kubernetes**, which allows Spark to use the **Kubernetes API as its master**.

The Spark **driver** can be:

- A Pod inside the Kubernetes cluster, or

- An external script (typically Python + PySpark) running outside Kubernetes, connected to it.

In my case, I use a **custom Spark driver image** built with the following Dockerfile:

```Dockerfile
FROM python:3.10-slim

# INSTALL SYSTEM DEPENDENCIES
RUN apt-get update && apt-get install -y \
    curl \
    bash \
    default-jdk \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

# AUTOMATICALLY DETECTS JAVA PATHS:
ENV JAVA_HOME=/usr/lib/jvm/default-java
ENV PATH=$JAVA_HOME/bin:$PATH

# ADD SPARK AND JUPYTER
ENV SPARK_VERSION=3.5.6 \
    HADOOP_VERSION=3 \
    SPARK_HOME=/opt/spark \
    PATH=/opt/spark/bin:$PATH \
    PYSPARK_PYTHON=python3 \
    PYSPARK_DRIVER_PYTHON=jupyter \
    PYSPARK_DRIVER_PYTHON_OPTS="lab --ip=0.0.0.0 --port=8888 --no-browser --allow-root"

# INSTALL NECESSARY PYTHON LIBRARIES
RUN pip install --upgrade pip \
 && pip install jupyterlab pyspark==${SPARK_VERSION}

# DOWNLOADS AND INSTALLS SPARK
RUN curl -fsSL https://downloads.apache.org/spark/spark-${SPARK_VERSION}/spark-${SPARK_VERSION}-bin-hadoop${HADOOP_VERSION}.tgz \
 | tar -xz -C /opt \
 && mv /opt/spark-${SPARK_VERSION}-bin-hadoop${HADOOP_VERSION} $SPARK_HOME

# EXPOSE JUPYTERLAB PORT
EXPOSE 8888

# SETS THE WORK DIRECTORY
WORKDIR /workspace

# ADD MY SQL CONNECTOR (I downloaded and putted it on the same directory on my laptop as the one where the Dockerfile exists, this way ADD using it on the image creation)
ADD mysql-connector-j-8.0.33.jar /opt/spark/jars/

# MAINTAINS THE POD ALIVE BECAUSE IT IS NOT A CLOSED EXECUTION
CMD ["tail", "-f", "/dev/null"]


# WITH THAT CREATED WE SIMULTANEOUSLY CREATE THE IMAGE AND PUSH IT TO A REPOSITORY (in this case mine, xavier418/spark-driver:4.0.0 --push):
```

This image is also available publicly on my **Docker Hub** repository under the name xavier418/spark-driver:spark-3.5.6-python-3.10 

To deploy it inside Kubernetes, I use the following deployment yaml manifest:

```yaml
spec:
  replicas: 1
  selector:
    matchLabels:
      app: spark-driver
  template:
    metadata:
      labels:
        app: spark-driver
    spec:
      serviceAccountName: spark-driver
      nodeSelector:
        node-role.kubernetes.io/worker: worker
      containers:
      - name: spark-driver
        image: xavier418/spark-driver:spark-3.5.6-python-3.10
        imagePullPolicy: Always
        ports:
        - containerPort: 8888
        - containerPort: 7077
        env: # These env variable are used because they allow us to use an Iceberg REST catalog, further details later on but basically this catalog won't inherit the spark configs for accessing S3, instead in uses the Amazon bundle SDK 2.20 for accessing and this bundle in turn searches for the credentials inside environment variables instead of the spark session itself.  
        - name: AWS_ACCESS_KEY_ID
          value: "wGyHqcx43ZGp86XtqWm8"
        - name: AWS_SECRET_ACCESS_KEY
          value: "89DlA5N6PfNE8o0XptU9zby6niweY2mXZkNbFsl4"
        - name: AWS_REGION # Region makes no sense because we are using MinIO, MinIO will ignore it when the Amazon SDK sends it but the SDK needs to find it, otherwise it gives error. But we could change "us-east-1" by whatever value comes to our mind first.
          value: "us-east-1"
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: spark-cluster-admin-binding
  namespace: default
subjects:
- kind: ServiceAccount
  name: spark-driver
  namespace: default
roleRef:
  kind: ClusterRole
  name: cluster-admin
  apiGroup: rbac.authorization.k8s.io
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: spark-driver
  namespace: default
---
apiVersion: v1
kind: Service
metadata:
  name: spark-driver-jupyter
  namespace: default
spec:
  type: NodePort
  selector:
    app: spark-driver
  ports:
    - port: 8888
      targetPort: 8888
      nodePort: 30088
---
apiVersion: v1
kind: Service
metadata:
  name: spark-driver-headless
  namespace: default
spec:
  clusterIP: None
  selector:
    app: spark-driver
  ports:
    - port: 7077
      targetPort: 7077
---
apiVersion: v1
kind: Service
metadata:
  name: spark-ui
  namespace: default
spec:
  type: NodePort
  selector:
    app: spark-driver
  ports:
    - protocol: TCP
      port: 4040
      targetPort: 4040
      nodePort: 30089
```

The image includes **JupyterLab, Spark, and Python** preinstalled, besides the mysql driver since we will use MySQL as metadata store for the Iceberg REST catalog. The Kubernetes Node Ports let us use our browser to look into diiferent Spark components such as **Spark UI, or Jupyter Lab**.

# Accessing JupyterLab

Once the Spark driver pod is deployed and ready, I connect to it using:

```bash
kubectl exec -it <spark-driver-pod> -n default -- bash
```

Then, I start Jupyter Lab with:

```bash
jupyter lab --ip=0.0.0.0 --port=8888 --allow-root --no-browser
```

This **exposes** JupyterLab through the node port **30088** defined in spark-driver.yaml.
From any machine on my local network, before doing it, look for the **generated token** on the jupyter lab output and **don't close** the console you are using:

![Kubernetes architecture](token.png)

This setup allows me to develop and execute Spark notebooks interactively inside Kubernetes from my local environment.

We just have to open our browser and navigate to the 30088 port of **no matter what node of our Kubernetes cluster** and paste the token, and we will be inside jupyter lab ready to start developing the **PySpark scripts**:

![Kubernetes architecture](jupyter.png)

# Creating the Bronze and Silver layers

To create **Bronze and Silver** layers and tables in Iceberg format, we use the following **Jupyter Notebook**:

```python
from pyspark.sql.functions import to_json, struct, col, expr, row_number, from_json, get_json_object, explode, when, regexp_replace
from pyspark.sql.window import Window
from pyspark.sql.types import StructType, StringType, IntegerType, MapType, StructField, LongType, TimestampType
from pyspark.sql import SparkSession
from pyspark.sql.functions import to_json, struct, col, expr, row_number, from_json, get_json_object, explode, when
from pyspark.sql.window import Window
from pyspark.sql.types import StructType, StringType, IntegerType, MapType, StructField, LongType, TimestampType, BooleanType, ArrayType
from pyspark.sql import SparkSession

spark = (
    SparkSession.builder
    .appName("JupyterSparkApp")
    .master("k8s://https://192.168.1.150:6443") # The kubernetes API and the port is listens from
    .config("spark.submit.deployMode", "client") # We run this SparkSession from this interactive notebook so no cluster mode
    .config("spark.driver.host", "spark-driver-headless.default.svc.cluster.local") # This headless service whereby executors communicate with the driver
    .config("spark.driver.port", "7077") # The incoming traffic goes to this headless service port
    .config("spark.driver.bindAddress", "0.0.0.0") # Driver is listening all possible IPs
    .config("spark.executor.instances", "2") # Number of spark executors created as pods 
    .config("spark.kubernetes.container.image", "apache/spark:3.5.6-scala2.12-java17-python3-r-ubuntu") # Image that will be used inside the executor pods
    .config("spark.kubernetes.executor.deleteOnTermination", "true") # Delete executor pods once the Spark session is deleted 
    .config("spark.kubernetes.executor.nodeSelector.node-role.kubernetes.io/worker", "worker") # Node affinity, I don't want executors to be created in controlplane
    .config("spark.jars.packages", "org.apache.iceberg:iceberg-spark-runtime-3.5_2.12:1.9.2," # jars to download for Iceberg
                                   "org.projectnessie.nessie-integrations:nessie-spark-extensions-3.5_2.12:0.103.3," # jars to download for nessie
                                   "org.apache.hadoop:hadoop-aws:3.3.4," # jars for S3
                                   "com.amazonaws:aws-java-sdk-bundle:1.12.262," # More jars for S3
                                   "org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.1") # Jars dowloaded for kafka
    .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions," # Allows us to extend Spark SQL syntax using Iceberg
                                    "org.projectnessie.spark.extensions.NessieSparkSessionExtensions") # Allows us to extend Spark SQL syntax using Nessie
    # Nessie catalog
    .config("spark.sql.catalog.nessie", "org.apache.iceberg.spark.SparkCatalog") # We create the Saprk catalog names nessie
    .config("spark.sql.catalog.nessie.catalog-impl", "org.apache.iceberg.nessie.NessieCatalog") # The type of the nessie catalog will be NessieCatalog type
    .config("spark.sql.catalog.nessie.uri", "http://nessie.nessie-ns.svc.cluster.local:19120/api/v1") # The nessie http endpoint is a serive created from the nessie helm chart 
    .config("spark.sql.catalog.nessie.ref", "main") # The nessie initial branch will be "main"
    .config("spark.sql.catalog.nessie.authentication.type", "NONE") # There's no defined authorization to access the nessie through the http endpoint 
    .config("spark.sql.catalog.nessie.warehouse", "s3a://synthetic") # The bucket (in this case its root folder) where the nessie will write its tables data  
    # S3 configuration
    .config("spark.hadoop.fs.s3a.access.key", "wGyHqcx43ZGp86XtqWm8") # S3 (MinIO) access key
    .config("spark.hadoop.fs.s3a.secret.key", "89DlA5N6PfNE8o0XptU9zby6niweY2mXZkNbFsl4") # S3 (MinIO) secret key
    .config("spark.hadoop.fs.s3a.endpoint", "http://myminio-hl.minio-tenant.svc.cluster.local:9000") # S3 (MinIO) protocol, kubernetes service and port to use let Sparl write and read from MinIO.
    .config("spark.hadoop.fs.s3a.path.style.access", "true") # The correct way to intepret MinIO folder for Spark
    .getOrCreate()
)

spark.sql("CREATE NAMESPACE IF NOT EXISTS nessie.data")
# NAMESPACE is Nessie's counterpart of a database in Hive/Haddop and a lot of other Catalogs.

spark.sql("DROP TABLE IF EXISTS nessie.data.orders_bronze")
# Just in case we rerun the script, we also need to eliminate all files from S3 ONLY AFTER DELETING NESSIE METADATA FROM THE TABLE

# Bronze layer products

# Read a continuos data stream by microbatches
# the kafka bootstrap service and port
# Corresponding topic
# Iput it but will be overriden by Spark's checkpoints
df = spark.readStream \
    .format("kafka") \
    .option("kafka.bootstrap.servers", "my-cluster-kafka-bootstrap.kafka.svc.cluster.local:9092") \
    .option("subscribe", "mongo.synthetic.products") \
    .option("startingOffsets", "earliest") \
    .load()

# Minor data type transformations and column filtering 
df_iceberg = df.select(
    col("key").cast("string"),
    col("value").cast("string"),
    "topic",
    "partition",
    "offset",
    "timestamp",
    "timestampType"
)

spark.sql("""
CREATE TABLE IF NOT EXISTS nessie.data.products_bronze (
    key STRING,
    value STRING,
    topic STRING,
    partition INT,
    offset LONG,
    timestamp TIMESTAMP,
    timestampType INT
)
USING iceberg
PARTITIONED BY (days(timestamp)) 
""") 
# We create a taylored custom-built table for the information coming from kafka as is according the Bronze layer philosophy of the medalion lakehouse
# Usually the best partitioning key to use is a time based key, the more data it is generate through time, the more equally sized partitions will be generated 
# to store it

# We insert info respecting the tables format
# Only append we rows 
# The checkpoint saves the last (and most likely all previous) record Spark read from the stream so it doesn't duplicate info twice on the target
# we save info in the nessie tabl above created
query = df_iceberg.writeStream \
    .format("iceberg") \
    .outputMode("append") \
    .option("checkpointLocation", "s3a://synthetic/checkpoints/products_bronze") \
    .toTable("nessie.data.products_bronze")
query.awaitTermination()
# When the process starts, we can stop the cell execution but the underlying Spark stream will still be active monitoring the Debezium topic

# Bronze layer orders
# This process does exactly the same as what we have seen above only now it's time for the orders to be processed
df = spark.readStream \
    .format("kafka") \
    .option("kafka.bootstrap.servers", "my-cluster-kafka-bootstrap.kafka.svc.cluster.local:9092") \
    .option("subscribe", "mongo.synthetic.orders") \
    .option("startingOffsets", "earliest") \
    .load()

df_iceberg = df.select(
    col("key").cast("string"),
    col("value").cast("string"),
    "topic",
    "partition",
    "offset",
    "timestamp",
    "timestampType"
)

spark.sql("""
CREATE TABLE IF NOT EXISTS nessie.data.orders_bronze (
    key STRING,
    value STRING,
    topic STRING,
    partition INT,
    offset LONG,
    timestamp TIMESTAMP,
    timestampType INT
)
USING iceberg
PARTITIONED BY (days(timestamp))
""")

query = df_iceberg.writeStream \
    .format("iceberg") \
    .outputMode("append") \
    .option("checkpointLocation", "s3a://synthetic/checkpoints/orders_bronze") \
    .toTable("nessie.data.orders_bronze")
query.awaitTermination()

# Just verifying everything went as expected
query = spark.sql("""
       SELECT * FROM nessie.data.orders_bronze
    """)
query.show()

# Same as above
query = spark.sql("""
   SELECT * FROM nessie.data.products_bronze
""")
query.show()

# In case we rerun the script, we also need to eliminate all files from S3
spark.sql("DROP TABLE IF EXISTS nessie.data.orders_silver")

# Silver layer
spark.sql("""
CREATE TABLE IF NOT EXISTS nessie.data.orders_silver (
  oid STRING,
  order_id STRING,
  order_date TIMESTAMP,
  is_rebate BOOLEAN,
  products ARRAY<STRUCT<
    product_id: STRING,
    product_price: STRING,
    product_quantity: STRING
>>,
  supplier_id STRING,
  supplier_name STRING,
  supplier_email STRING,
  customer_id STRING,
  customer_name STRING,
  email STRING,
  customer_genre STRING,
  age INTEGER,
  cell_phone STRING,
  address_id STRING,
  address_name STRING,
  city STRING,
  zip_code STRING,
  state_region STRING,
  country STRING,
  op STRING,
  ts_ms BIGINT
  
)
USING iceberg
PARTITIONED BY (days(order_date))
TBLPROPERTIES (
  'format-version' = '2',
  'read.change.data.capture' = 'true'
)
""")

# The previous partition criteria holds true for this case too, in this table we are going to put all information coming from the orders
# in a single broad table with columns for all information.


df_bronze = spark.readStream.format("iceberg").table("nessie.data.orders_bronze") # We start reading new rows from the bronze table in microbatches 
"""
Clarification:

With MapType labels are assigned dinamically that's why spark finds "before" or "after" without specifying it inside the payload_schema:
- Faster when we see JSONs with few nested levels and a lot of fields in each level and no ArrayTypes

StructType, nevertheless,  is insesible to the order in which StructFields appear on the StructType, It will search for the label we put 
at the beginning like "id" on StructField("id", StructType([StructField("oid", StringType)]).
- Good for any kind of JSON but slower to define JSON schemas with
"""
payload_schema = StructType().add("payload", MapType(StringType(), StringType()))
oid_schema = StructType([StructField("$oid", StringType())])

df_parsed = df_bronze.selectExpr("CAST(value AS STRING) as json_str", "CAST(key AS STRING) as json_key") \
    .withColumn("value", from_json(col("json_str"), payload_schema)) \
    .withColumn("key", from_json(col("json_key"), payload_schema)) \
    .withColumn("key_id", from_json(col("key.payload")["id"], oid_schema)) \
    .select("value", "key_id")

# Notice how once we have the JSON schema defined using the from_json function on the columns that contain JSONs Spark becomes aware of the structure
# and can draw anything we want from the JSON regardless of the nesting level:
df_parsed_cols = df_parsed.select(
    col("key_id.$oid").alias("oid"),
    col("value.payload")["before"].alias("before"),
    col("value.payload")["after"].alias("after"),
    col("value.payload")["source"].alias("source"),
    col("value.payload")["op"].alias("op"),
    col("value.payload")["ts_ms"].alias("ts_ms"),
    col("value.payload")["transaction"].alias("transaction"))

orders_schema = StructType([
    StructField("_id", StructType([StructField("$oid", StringType())])),
    StructField("order_id", StringType()),
    StructField("order_date",TimestampType()),
    StructField("is_rebate", BooleanType()),
    StructField("comercial_partner", StructType([StructField("supplier_id", StringType(), nullable=True), 
                                                 StructField("supplier_name", StringType(), nullable=True),
                                                 StructField("supplier_email", StringType(), nullable=True),
                                                 StructField("customer_id", StringType(), nullable=True),
                                                 StructField("customer_name", StringType(), nullable=True),
                                                 StructField("email", StringType(), nullable=True),
                                                 StructField("customer_genre", StringType(), nullable=True),
                                                 StructField("age", IntegerType(), nullable=True),
                                                 StructField("cell_phone", StringType()),
                                                 StructField("address", StructType([StructField("address_id", StringType()),
                                                                                    StructField("address_name", StringType()),
                                                                                    StructField("city", StringType()),
                                                                                    StructField("zip_code", StringType()),
                                                                                    StructField("state_region", StringType()),
                                                                                    StructField("country", StringType())
                                                             ]))])),
    StructField("products", ArrayType(StructType([StructField("product_id", StringType()),
                                                  StructField("product_price", StringType()),
                                                  StructField("product_quantity", StringType())
                                                  ])))
])

df_parsed_nested = df_parsed_cols.withColumn("data_map", from_json(col("after"), orders_schema))

df_BIGINT = df_parsed_nested.withColumn("ts_ms", col("ts_ms").cast("bigint"))

df_map = df_BIGINT.select(
    col("oid"),
    col("data_map")["order_id"].alias("order_id"),
    col("data_map")["order_date"].alias("order_date"),
    col("data_map")["is_rebate"].alias("is_rebate"),
    col("data_map")["products"].alias("products"),
    col("data_map")["comercial_partner"]["supplier_id"].alias("supplier_id"),
    col("data_map")["comercial_partner"]["supplier_name"].alias("supplier_name"),
    col("data_map")["comercial_partner"]["supplier_email"].alias("supplier_email"),
    col("data_map")["comercial_partner"]["customer_id"].alias("customer_id"),
    col("data_map")["comercial_partner"]["customer_name"].alias("customer_name"),
    col("data_map")["comercial_partner"]["email"].alias("email"),
    col("data_map")["comercial_partner"]["customer_genre"].alias("customer_genre"),
    col("data_map")["comercial_partner"]["age"].alias("age"),
    col("data_map")["comercial_partner"]["cell_phone"].alias("cell_phone"),
    col("data_map")["comercial_partner"]["address"]["address_id"].alias("address_id"),
    col("data_map")["comercial_partner"]["address"]["address_name"].alias("address_name"),
    col("data_map")["comercial_partner"]["address"]["city"].alias("city"),
    col("data_map")["comercial_partner"]["address"]["zip_code"].alias("zip_code"),
    col("data_map")["comercial_partner"]["address"]["state_region"].alias("state_region"),
    col("data_map")["comercial_partner"]["address"]["country"].alias("country"),
    col("op").alias("op"),
    col("ts_ms"), 
)
"""
NOTICE HOW WE COULD ADD THIS LINE AT THE SPARK TRANSFORMATION ABOVE TO COMPLETELY NORMALIZE THE DATAFRAME:

explode(col("data_map")["products"]).alias("products") These transformation wewdon't use it

AND THEN ADD A NEW STEP WHERE WE CAN EXTRACT THE PRODUCT, NAME AND QUANTITY IN DIFFERENT COLUMNS: 
 df_map = df_ex.select(
    "*",
   col("products")["product_id"].alias("product_id"),
   col("products")["product_price"].alias("product_name"),
   col("products")["product_quantity"].alias("quantity")
 )
HOWEVER, WE WON'T EXPLODE THE COLUMNS BECAUSE THAT WAY WE PRESERVE THE CORRESPODENCE BETWEEN A ROWS AND ORDERS, THAT WILL HELP US LATER ON 
WITH GOLDEN TABLES MAKING THE OVERALL PROCESS OF COMING UP WITH SEPARATE GOLDEN TABLES EASIER FOR US. BESIDES, IN THE LAST PATH TO GOLD TABLES
WE CAN MAKE USE OF THIS TRANSFORMATION ABOVE TO EXPLODE THE FORM A NEW products_orders TABLE.
"""
def merge_into_orders_silver(microBatchOutputDF, batchId): 
    # This function will be put into forEachBatch what will allows us to treat each microbatch as a regular batch job and we therefore apply 
    # batch functions not allowed in stremas without losing the underlyinf streaming structure 

    window = Window.partitionBy("oid").orderBy(col("ts_ms").desc())

    df_final_batch = microBatchOutputDF.withColumn("rn", row_number().over(window)) \
                      .filter(col("rn") == 1) \
                      .drop("rn")
    
    df_final_batch.createOrReplaceTempView("batch_updates")

    microBatchOutputDF.sparkSession.sql("""
        MERGE INTO nessie.data.orders_silver AS target
        USING batch_updates AS source
        ON target.oid = source.oid
        WHEN MATCHED AND source.op = 'd' AND source.ts_ms > target.ts_ms THEN DELETE
        WHEN MATCHED AND source.op IN ('u', 'r', 'i') AND source.ts_ms > target.ts_ms THEN UPDATE SET *
        WHEN NOT MATCHED AND source.op IN ('c', 'r', 'i') THEN INSERT *
    """)
""" 
 Hopefully, thanks to the clarity of the MERGE INTO syntax it becomes really clear what we are doing with each micro-batch, what this does is
 Recreating the current state of the orders document in MongoDB in the form of a table. For every MongoDB change, an equivalent change takes place 
 in this table. 
 Only take into account that if the connector gets disconnected from MongoDB and in that time some data gets deleted, the corresponding rows
 won't be erased from this table, but this can be easily tackled by doing periodic inner joins between this table and a Spark batch job that 
 also reconstructs the state of the table. Nevertheless, it would imply a failure in the fault tolerance and high availability properties of the
 data pipeline but we could easily make more Kafka Connect pods to achieve HA. 
"""

query = df_map.writeStream \
    .foreachBatch(merge_into_orders_silver) \
    .option("checkpointLocation", "s3a://synthetic/checkpoints/orders_silver") \
    .outputMode("update") \
    .start()

spark.sql("SELECT * FROM nessie.data.orders_silver").show() # Verify everything went okay

spark.sql("DROP TABLE IF EXISTS nessie.data.products_silver").show() # In case we rerun the script, we also need to eliminate all files from S3

# Silver layer
spark.sql("""
CREATE TABLE IF NOT EXISTS nessie.data.products_silver (
  oid STRING,
  product_id STRING,
  product_name STRING,
  product_price STRING,
  product_description STRING,
  type_id STRING,
  type_name STRING,
  type_description STRING,
  op STRING,
  ts_ms BIGINT
)
USING iceberg
PARTITIONED BY (bucket(16, product_id)) -- 16 buckets
TBLPROPERTIES (
  'format-version' = '2',
  'read.change.data.capture' = 'true'
)
""")
# Here expect a fixed or almost constant quantity of products, so the partitioning can be a fixed bucketing using product_id as partition key

df_bronze = spark.readStream.format("iceberg").table("nessie.data.products_bronze")

payload_schema = StructType().add("payload", MapType(StringType(), StringType()))
oid_schema = StructType([StructField("$oid", StringType())])


df_parsed = df_bronze.selectExpr("CAST(value AS STRING) as json_str", "CAST(key AS STRING) as json_key") \
    .withColumn("value", from_json(col("json_str"), payload_schema)) \
    .withColumn("key", from_json(col("json_key"), payload_schema)) \
    .withColumn("key_id", from_json(col("key.payload")["id"], oid_schema)) \
    .select("value", "key_id")

df_parsed_cols = df_parsed.select(
    col("key_id.$oid").alias("oid"),
    col("value.payload")["before"].alias("before"),
    col("value.payload")["after"].alias("after"),
    col("value.payload")["source"].alias("source"),
    col("value.payload")["op"].alias("op"),
    col("value.payload")["ts_ms"].alias("ts_ms"),
    col("value.payload")["transaction"].alias("transaction"))

product_type_schema = StructType([
    StructField("type_id", StringType()),
    StructField("type_name", StringType()),
    StructField("type_description", StringType())
])

product_schema = StructType([
    StructField("_id", StructType([StructField("$oid", StringType())])),
    StructField("product_id", StringType()),
    StructField("product_name", StringType()),
    StructField("product_price", StringType()),
    StructField("product_description", StringType()),
    StructField("product_type", product_type_schema)
])
    
df_if = df_parsed_cols.withColumn("data_map", from_json(col("after"), product_schema))

df_BIGINT = df_if.withColumn("ts_ms", col("ts_ms").cast("bigint"))

df_final = df_BIGINT.select(
        col("oid"),
        col("data_map")["product_id"].alias("product_id"),
        col("data_map")["product_name"].alias("product_name"),
        col("data_map")["product_price"].alias("product_price"),
        col("data_map")["product_description"].alias("product_description"),
        col("data_map")["product_type"]["type_id"].alias("type_id"),
        col("data_map")["product_type"]["type_name"].alias("type_name"),
        col("data_map")["product_type"]["type_description"].alias("type_description"),
        col("op").alias("op"),
        col("ts_ms")
    )

# YOU NEED A FUNCTION THAT LOOKS AT ts_ms AND TAKES THE MOST RECENT RSTATE OF EACH PRODUCT DEPENDING ON WHETHER IT IS AN UPDATE A DELETE OR AN INSERT. 
# WE WILL USE BTACH SYNTAX AND FUNCTIONALITIES FOR EACH STREAMING BATCH TO HEKP US LIKE WE CAN SEE ON THE FOLLOWING CODE
def merge_into_products_silver(microBatchOutputDF, batchId):

    window = Window.partitionBy("oid").orderBy(col("ts_ms").desc())

    df_final_batch = microBatchOutputDF.withColumn("rn", row_number().over(window)) \
                      .filter(col("rn") == 1) \
                      .drop("rn")
    
    df_final_batch.createOrReplaceTempView("batch_updates")

    microBatchOutputDF.sparkSession.sql("""
        MERGE INTO nessie.data.products_silver AS target
        USING batch_updates AS source
        ON target.oid = source.oid
        WHEN MATCHED AND source.op = 'd' AND source.ts_ms > target.ts_ms THEN DELETE
        WHEN MATCHED AND source.op IN ('u', 'r', 'i') AND source.ts_ms > target.ts_ms THEN UPDATE SET *
        WHEN NOT MATCHED AND source.op IN ('c', 'r', 'i') THEN INSERT *
    """)
    
#    microBatchOutputDF.sparkSession.sql("""
#        DELETE FROM nessie.products_silver AS target
#        WHERE target.product_id NOT IN (SELECT product_id FROM batch_updates) 
#        AND target.oid NOT IN (SELECT oid FROM batch_updates)
#    """)

# THIS LAST ADDITION COULD ALSO REMOVE THE NEED FOR PERIODIC INNER JOIN BATCH JOBS BUT ONLY BY DOING EXACTLY THAT WITH EVERY SINGLE MICROBATCH, 
# THAT'S EXTREMELY INNEFICIENT, EVERY BATCH WOULD REQUIRE SPARK TO SCAN ALL THE products_silver TABLE TO COMPARE EVERY POSSIBLE VALUE
# OF oid AND product_id, WITH THE FEW VALUES OF oid AND product_id INSIDE EACH MICROBATCH. THE ALTERNATIVE WOULD COMPARE ONE SINGLE TIME 
# ALL POSSIBLE DELETED RECORDS.

query = df_final.writeStream \
    .foreachBatch(merge_into_products_silver) \
    .option("checkpointLocation", "s3a://synthetic/checkpoints/products_silver") \
    .outputMode("update") \
    .start()
query.awaitTermination()

query = spark.sql("""
       SELECT * FROM nessie.data.products_silver
    """)
query.show(30) # Verifycation checkpoint

# THESE THREE LINES BELOW ARE USED TO CLOSE THE OPENED STREAMS ABOVE WHEN WE WHAT TO EITHER STOP THE SPARK STREAMS OR WE WANT TO STOP THE SPARK SESSION ALTOGETHER.
for q in spark.streams.active:
    q.stop()

spark.streams.active

spark.stop()
```
You can see the corresponding .ipynb script with it's outputs in this notebook:

![Kubernetes architecture](Bronze+Silver.ipynb)

# Gold layer 

Now, it is the time to explain how I have approached the **Gold layer**.
Up until now we have an arquitecture with two tables, orders_silver and products_silver, that update themselves in **near real time** with microbatches using **Spark Structured Streaming** our goal for the gold layer is simply to **model this data** in these two tables and **shape it** according the **star schema**, the most usual structure **Power BI semantic models** have. Ideally, we also want to **avoid data duplication and reprocessing** of this data one more time to save costs in production while upgrading it from the silver to the gold layer, which would involve yet another Spark stream transporting microbatches from silver Iceberg tables to gold Iceberg tables, let alone the fact that we cannot make CDC changes from these tables using Spark streams, (at least by the time I'm writing that, Iceberg supports creating tables with change awareness by setting a TBL property named capture.changes = 'true' or something alike, but **only works with Spark batches, not with Spark Structured Streaming**). 

Well, there's a simple and elegant solution that accomplishes everything. **Iceberg Views**:

- They won't duplicate data.

- They have the flexibility required to shape data in according to the **star schema**.

- These transformations, as we will see are primarily filters on a subset of columns and/or rows, and if we recall the fact that the **silver tables are partitioned (row-level partition)** and that they store data in **parquet files (column-level partitiong)** means that underlying queries will be very efficient in terms of processing and computing resources.

This last part of the implementation will require the following additional technologies:

- Iceberg REST catalog
- MySQL
- Trino
- Traefik

Let's explain what they do, Iceberg REST is a layer over another Icerberg catalog that exposes the API and let's us communicate with the underlying catalog using the Iceberg API, in my case, I will use a JDBC catalog under the hood, that's why we will use MySQL it will be the database metastore. 
Trino is a very popular distributed SQL engine and its purpose consists of being able to run SQL queries over known data lakehouses catalogs such as Nessie or Iceberg REST. It also comes with the hability to create views and tables upon the existing data it sees and store them on the same catalogs it is looking at. 

I think its a good idea to clarify how it is different from Spark because, some of their capabilities certainly overlap to some extent. The focus of Spark lies on **data transformation** it can transform data with using **SQL statements thanks to spark.sql** but we can also use USF's and overall **underlying python environment to use the programatic capabilities** of such a powerful language like loops to iterate while transforming, it can also **transform semistructured data** as we saw like JSONs. On the other hand, Trino is purely SQL-based, and within the scope of what SQL allows, it can transform or create data or views, but it's main purpose is to **unify different data sources under a single SQL-ready environment** and give the **capabilities of a regular relational database** like users, concurrency, TLS encryption, and more.

**Traefik** is just another service like **nginx** that allows us to use **TLS encryption** for in and outbound traffic for the Kubernetes cluster, it opens us a Load Balancer that will redirect traffic using an Ingress whereby we will access the service from outside the kubernetes cluster. 


# Deploy a relational database to host Iceberg REST metadata

**First of all, why Iceberg REST?** Don't we have Nessie?

Of course, and let me add that course was plan A, but apparently the **Trino's views are not supported on Nessie** even though tables are so we will use the Iceberg REST catalog because **Trino does support views for this REST catalog**. As we said before, **Iceberg REST will store its metadata inside this MySQL database.**

Before we deploy Iceberg we need to deploy MySQL database because Iceberg REST will look for it as soon as it is deployed and will fail if it cannot connect to it.

So, to deploy the databse we do the following 
💡 Note: The storage ended up being **ephemeral** because this catalog will only store views, not tables, of course Iceberg REST views metadata comes in the form of data inside MySQL but **views are stateless** objects, and I already have a script to generate them in Trino:

```yaml
# MySQL Secret
# TO CONNECT ONCE DEPLOYED: kubectl run mysql-client --rm -it --image=mysql:8.1 -- mysql -h mysql -u iceberg -picebergpass iceberg_catalog
apiVersion: v1
kind: Secret
metadata:
  name: mysql-secret
  namespace: default
type: Opaque
stringData:
  MYSQL_ROOT_PASSWORD: "rootpassword"
  MYSQL_USER: "iceberg"
  MYSQL_PASSWORD: "icebergpass"
  MYSQL_DATABASE: "iceberg_catalog"

---
# MySQL Deployment
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mysql
  namespace: default
spec:
  replicas: 1
  selector:
    matchLabels:
      app: mysql
  template:
    metadata:
      labels:
        app: mysql
    spec:
      containers:
      - name: mysql
        image: mysql:8.1
        env:
        - name: MYSQL_ROOT_PASSWORD
          valueFrom:
            secretKeyRef:
              name: mysql-secret
              key: MYSQL_ROOT_PASSWORD
        - name: MYSQL_USER
          valueFrom:
            secretKeyRef:
              name: mysql-secret
              key: MYSQL_USER
        - name: MYSQL_PASSWORD
          valueFrom:
            secretKeyRef:
              name: mysql-secret
              key: MYSQL_PASSWORD
        - name: MYSQL_DATABASE
          valueFrom:
            secretKeyRef:
              name: mysql-secret
              key: MYSQL_DATABASE
        ports:
        - containerPort: 3306
        volumeMounts:
        - name: mysql-data
          mountPath: /var/lib/mysql
      volumes:
      - name: mysql-data
        emptyDir: {}  # ephemeral storage

---
# MySQL Service
apiVersion: v1
kind: Service
metadata:
  name: mysql
  namespace: default
spec:
  selector:
    app: mysql
  ports:
  - protocol: TCP
    port: 3306
    targetPort: 3306
  type: ClusterIP
```

You will everything went fine when you see this deployment with kubectl:

<img width="891" height="268" alt="image" src="https://github.com/user-attachments/assets/eb1e9e3d-82fd-4053-ad34-c30659bc07ed" />

If you also want to make sure it works you can login using, if you can enter, everything was properly set up:

```bash
 kubectl run mysql-client --rm -it --image=mysql:8.1 -- mysql -h mysql -u iceberg -picebergpass iceberg_catalog
```


# Icerberg REST Catalog

Now we can deploy Iceberg REST catalog, for that we will use the following yaml:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: iceberg-rest
  namespace: default
spec:
  replicas: 1
  selector:
    matchLabels:
      app: iceberg-rest
  template:
    metadata:
      labels:
        app: iceberg-rest
    spec:
      containers:
      - name: iceberg-rest
        image: xavier418/iceberg-rest-fixture:2.0
        imagePullPolicy: Always
        ports:
        - containerPort: 8181
        env:
        # MySQL config
        - name: CATALOG_URI
          value: "jdbc:mysql://mysql:3306/iceberg_catalog"
        - name: CATALOG_JDBC_USER
          value: "iceberg"
        - name: CATALOG_JDBC_PASSWORD
          value: "icebergpass"
        - name: CATALOG_JDBC_STRICT_MODE
          value: "true"
        - name: REST_PORT
          value: "8181"
        # S3 (MinIO) config
        - name: CATALOG_IO__IMPL
          value: "org.apache.iceberg.aws.s3.S3FileIO"
        - name: CATALOG_S3_ENDPOINT
          value: "http://myminio-hl.minio-tenant.svc.cluster.local:9000"
        - name: AWS_ACCESS_KEY_ID
          value: "wGyHqcx43ZGp86XtqWm8"
        - name: AWS_SECRET_ACCESS_KEY
          value: "89DlA5N6PfNE8o0XptU9zby6niweY2mXZkNbFsl4"
        - name: CATALOG_S3_PATH_STYLE_ACCESS
          value: "true"
        - name: CATALOG_WAREHOUSE
          value: "s3a://synthetic"
        - name: AWS_REGION
          value: "us-east-1"
        - name: CATALOG_S3_path__style__access
          value: "true"
---
# Iceberg REST Service
apiVersion: v1
kind: Service
metadata:
  name: iceberg-rest
  namespace: default
spec:
  selector:
    app: iceberg-rest
  ports:
  - protocol: TCP
    port: 8181
    targetPort: 8181
  type: ClusterIP
```
As you can see, there's a custom image, this image is the regular iceberg rest image we can find in the official apache/iceberg repo on docker hub, the only difference is that I added the mysql connector to that iceberg can communicate with MySQL deployed on the previous yaml.

The Dockerfile is the following one:

```Dockerfile
FROM apache/iceberg-rest-fixture:latest

# Copy MySQL JDBC driver, (needless to say the jar was in the same directory as this Dockerfile, as said previously, I think I used ADD instead of COPY, both should work fine)
COPY mysql-connector-j-8.1.0.jar /usr/lib/iceberg-rest/

# Run Iceberg REST with the driver in classpath
CMD ["java", "-cp", "iceberg-rest-adapter.jar:mysql-connector-j-8.1.0.jar", "org.apache.iceberg.rest.RESTCatalogServer"]
```

You have probably noticed how we **didn't even touch the Iceberg REST catalog** in PySpark code on the **Bronze and Silver layer section**, we didn't even configured the Spark Session to access it. It is because we will only access it through Trino, it is also possible to access **REST catalog with Spark** using this Spark session conf:

```python
from pyspark.sql.functions import to_json, struct, col, expr, row_number, from_json, get_json_object, explode, when, regexp_replace, current_timestamp
from pyspark.sql.window import Window
from pyspark.sql.types import StructType, StringType, IntegerType, MapType, StructField, LongType, TimestampType
from pyspark.sql import SparkSession
from pyspark.sql.functions import to_json, struct, col, expr, row_number, from_json, get_json_object, explode, when
from pyspark.sql.window import Window
from pyspark.sql.types import StructType, StringType, IntegerType, MapType, StructField, LongType, TimestampType, BooleanType, ArrayType
from pyspark.sql import SparkSession, Row

spark = (
    SparkSession.builder
    .appName("JupyterSparkApp")
    .master("k8s://https://192.168.1.150:6443") # The kubernetes API and the port is listens from
    .config("spark.submit.deployMode", "client") # We run this SparkSession from this interactive notebook so no cluster mode
    .config("spark.driver.host", "spark-driver-headless.default.svc.cluster.local") # This headless service whereby executors communicate with the driver
    .config("spark.driver.port", "7077") # The incoming traffic goes to this headless service port
    .config("spark.driver.bindAddress", "0.0.0.0") # Driver is listening all possible IPs
    .config("spark.executor.instances", "2") # Number of spark executors created as pods 
    .config("spark.kubernetes.container.image", "apache/spark:3.5.6-scala2.12-java17-python3-r-ubuntu") # Image that will be used inside the executor pods
    .config("spark.kubernetes.executor.deleteOnTermination", "true") # Delete executor pods once the Spark session is deleted 
    .config("spark.kubernetes.executor.nodeSelector.node-role.kubernetes.io/worker", "worker") # Node affinity, I don't want executors to be created in controlplane
    .config("spark.jars.packages", "org.apache.iceberg:iceberg-spark-runtime-3.5_2.12:1.9.2," # jars to download for Iceberg
                                   "org.projectnessie.nessie-integrations:nessie-spark-extensions-3.5_2.12:0.103.3," # jars to download for nessie
                                   "org.apache.hadoop:hadoop-aws:3.3.4," # jars for S3
                                   "software.amazon.awssdk:bundle:2.20.147," # More jars for S3
                                   "org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.1") # Jars dowloaded for kafka
    .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions," # Allows us to extend Spark SQL syntax using Iceberg
                                    "org.projectnessie.spark.extensions.NessieSparkSessionExtensions") # Allows us to extend Spark SQL syntax using Nessie
    # Executor environment variables for AWS SDK
    .config("spark.executorEnv.AWS_REGION", "us-east-1")
    .config("spark.executorEnv.AWS_ACCESS_KEY_ID", "wGyHqcx43ZGp86XtqWm8")
    .config("spark.executorEnv.AWS_SECRET_ACCESS_KEY", "89DlA5N6PfNE8o0XptU9zby6niweY2mXZkNbFsl4")
    # REST catalog
    .config("spark.sql.catalog.rest", "org.apache.iceberg.spark.SparkCatalog")
    .config("spark.sql.catalog.rest.catalog-impl", "org.apache.iceberg.rest.RESTCatalog")
    .config("spark.sql.catalog.rest.uri", "http://iceberg-rest.default.svc.cluster.local:8181")
    .config("spark.sql.catalog.rest.warehouse", "s3://synthetic")
    .config("spark.sql.catalog.rest.io-impl", "org.apache.iceberg.aws.s3.S3FileIO")
    .config("spark.sql.catalog.rest.s3.access-key", "wGyHqcx43ZGp86XtqWm8")
    .config("spark.sql.catalog.rest.s3.secret-key", "89DlA5N6PfNE8o0XptU9zby6niweY2mXZkNbFsl4")
    .config("spark.sql.catalog.rest.s3.endpoint", "http://myminio-hl.minio-tenant.svc.cluster.local:9000")
    .config("spark.sql.catalog.rest.s3.path-style-access", "true")
    # S3 configuration
    .config("spark.hadoop.fs.s3a.region", "us-east-1")
    .config("spark.hadoop.fs.s3a.access.key", "wGyHqcx43ZGp86XtqWm8") # S3 (MinIO) access key
    .config("spark.hadoop.fs.s3a.secret.key", "89DlA5N6PfNE8o0XptU9zby6niweY2mXZkNbFsl4") # S3 (MinIO) secret key
    .config("spark.hadoop.fs.s3a.endpoint", "http://myminio-hl.minio-tenant.svc.cluster.local:9000") # S3 (MinIO) protocol, kubernetes service and port to use let Sparl write and read from MinIO.
    .config("spark.hadoop.fs.s3a.path.style.access", "true") # The correct way to intepret MinIO folder for Spark
    .getOrCreate()
)
# You can try command like:
spark.sql("CREATE DATABASE IF NOT EXISTS rest.gold")

spark.sql("SHOW DATABASES IN rest").show()
```

Take into account that objects like **DATABASES**, called **SCHEMAS in Trino**, as well as **TABLES** created in Spark will be **perfectly compatible and visible** by Trino and viceversa, **but that is not the case with VIEWS.**

# Traefik 

To create we will use the official Helm Chart like that, running this command on the controlplane:

```bash
helm install traefik traefik/traefik -n traefik --create-namespace
```
For our use case we don't need to change any of the default values, this will create the folowing:


<img width="873" height="238" alt="image" src="https://github.com/user-attachments/assets/eb53f829-e202-4171-94db-62239dd58d29" />

# Trino

Now that we have Traefik we can deploy Trino using TLS and Password, it is not really necessary but I thought it was a good exercise and allowed me to familiarize with Kubernetes Load Balancers and TLS encryption. 
So for TLS encryption we first thing we will use is the following command:

```bash
openssl req -x509 -nodes -days 365 \
  -newkey rsa:2048 \
  -keyout trino.key \
  -out trino.crt \
  -subj "/CN=trino.local/O=HomeLab"
```

This will generate the certificate **trino.crt** and the key **trino.key**, now we will use them to formulate a **Kubernetes secret** like that:

```bash
kubectl create secret tls trino-tls \
  --cert=trino.crt \
  --key=trino-key \
  -n trino
```

Once you have it, you will see the secret there (the rest of the secrets will not be visible yet) using:

```bash
kubectl get secret -n trino
```
<img width="652" height="118" alt="image" src="https://github.com/user-attachments/assets/b839131b-556a-4f8d-906d-eac766324575" />

Finally, we can deploy the Trino itself using the oficial Helm Chart, but first, we have to write the values.yaml file, the configuratoin we display on the yaml file is the following one:

```bash
catalogs:
  lakehouse: |-
    connector.name=iceberg
    iceberg.catalog.type=nessie
    iceberg.nessie-catalog.uri=http://nessie.nessie-ns.svc.cluster.local:19120/api/v1
    iceberg.nessie-catalog.default-warehouse-dir=s3://synthetic
    iceberg.nessie-catalog.ref=main
    fs.native-s3.enabled=true
    s3.region=local
    s3.endpoint=http://myminio-hl.minio-tenant.svc.cluster.local:9000
    s3.path-style-access=true
    s3.aws-access-key=wGyHqcx43ZGp86XtqWm8
    s3.aws-secret-key=89DlA5N6PfNE8o0XptU9zby6niweY2mXZkNbFsl4
  rest: |-
    connector.name=iceberg
    iceberg.catalog.type=rest
    iceberg.rest-catalog.uri=http://iceberg-rest.default.svc.cluster.local:8181
    fs.native-s3.enabled=true
    s3.region=us-east-1
    s3.endpoint=http://myminio-hl.minio-tenant.svc.cluster.local:9000
    s3.path-style-access=true
    s3.aws-access-key=wGyHqcx43ZGp86XtqWm8
    s3.aws-secret-key=89DlA5N6PfNE8o0XptU9zby6niweY2mXZkNbFsl4

# helm install trino-cluster trino/trino --namespace trino --create-namespace -f values.yaml

server:
  config:
    authenticationType: "PASSWORD" # TO USE THIS AUTHENTICATION TYPE "PASSWORD" TLS MUST BE ENABLED, WE CAN CREATE TLS INSIDE THE TRINO HTTP-SERVER ITSELF OR THE PROPERTY http-server.process-forwarded=true
  coordinatorExtraConfig: |-
    http-server.process-forwarded=true
    # THIS OPTION TELLS TRINO TO EXPECT TLS ENCRYPTION ON THE ENDPOINTS WHEREBY THE TRAFFIC WILL COME INSTEAD OF CREATING IT ITSELF. LIKE SAYING "EXPECT OUTSOURCED TLS"
    internal-communication.shared-secret=UNilVtC161yxNr9wl634iY9GnrenjgLx9cYkr0oAzL/P/ZbpJXftSo/qG4z5Xl/jsmhLQI7G6O5buzd9FHpKN5zxutjxsFyPz7NV3FHW1qaoFiSaC3r8XIFSo03w5seMV4DWugy6OZ7KhwQIV59+3Kx1hNgWg12ntmRj
  workerExtraConfig: |-
    internal-communication.shared-secret=UNilVtC161yxNr9wl634iY9GnrenjgLx9cYkr0oAzL/P/ZbpJXftSo/qG4z5Xl/jsmhLQI7G6O5buzd9FHpKN5zxutjxsFyPz7NV3FHW1qaoFiSaC3r8XIFSo03w5seMV4DWugy6OZ7KhwQIV59+3Kx1hNgWg12ntmRj
    # THIS SHARED SECRET MUST ALSO BE INCLUDED IF YOU WANT RO USE PASSWORD AND TLS PROPERTIES, BESIDES IT MUST BE PRESENT IN BOTH coordinator AND worker INSTANCES AS WE CAN SEE ABOVE.
auth:
  passwordAuth: "xavier:$2y$12$Nd88rwv86wEWpJdW0y1uBeN1KXBlsfrciuh4tN8Zx7t3JTmYMIw/u" # The password is 1234 but we have to use bcrypt, with a cost factor of 12, we can use a python script to generate it.

coordinator:
  affinity: # affinity VALUE FOLLOWS THE SAME GENERAL AFFINITY STRUCTURE AS ANY OTHER KUBERNETES YAML
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        nodeSelectorTerms:
          - matchExpressions:
              - key: kubernetes.io/hostname
                operator: In
                values:
                  - ubuntu
                  - ubuntu2

worker:
  affinity: # AND WE SCHEDULE BOTH TYPES OF TRINO PODS TO BE CREATED IN ONE OF THE WORKER NODES OF THE KUBERNETES SERVICE
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        nodeSelectorTerms:
          - matchExpressions:
              - key: kubernetes.io/hostname
                operator: In
                values:
                  - ubuntu
                  - ubuntu2
```

Once we have the values we deploy the helm chart using them:

```bash
helm install trino-cluster trino/trino --namespace trino --create-namespace -f values.yaml
```
And we will see the following Kubernetes components deployed on the namespace:

<img width="938" height="340" alt="image" src="https://github.com/user-attachments/assets/4d44c105-8a97-4925-9533-bfc7c111e2b8" />

And the last step that wraps it all up will be the creation of the Ingress that points to the service and port trino-cluster-trino:8080 from the traefik Load Balancer, we will create the Ingress like that:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: trino-ingress
  namespace: trino
  annotations:
    kubernetes.io/ingress.class: traefik # WE USE TRAEFIK DEPLOYMENT AND ITS LOAD BALANCER
spec:
  tls:
    - hosts:
        - trino.local # ENABLE TLS FOR THIS HOST
      secretName: trino-tls # THATS THE SECRET NAME THAT KUBERNETES WILL USE WITH THE trino.key AND trino.crt.
  rules:
    - host: trino.local # THE HOST TO USE
      http:
        paths:
          - path: / # THIS IS THE INEXISTENT PATH, ONLY USE trino.local ON THE BROWSER
            pathType: Prefix
            backend: # WHAT INNER KUBERNETES COMPONENT WILL BE CONNECTED TO THE HOSTPATH trino.local, THE SERVICE BELOW
              service:
                name: trino-cluster-trino
                port:
                  number: 8080
```

Now, one last step before we can use it. In the machine that will be used to connect to Trino we need to go to its hosts file and append a line like with the structure '(one of the Kubernetes nodes IPs)  trino.local'. **Not separated by spaces but by a Tab**. In my case with Windows we find it in **C:\Windows\System32\drivers\etc**:

<img width="831" height="803" alt="image" src="https://github.com/user-attachments/assets/4ebe66a0-7fbc-4367-b92e-d618fea68362" />

Apparently, it can work with Tab as well as spaces based on the rest of the matches, needless to say you should **not touch or erase any of the lines** there except if you know exactly what you're doing, as an example, in my case it seems **docker added a couple of matches** while it was first instaled.

# Trino client

Now we just have to **download** the trino client and **put it in a folder** like that:

<img width="1242" height="577" alt="image" src="https://github.com/user-attachments/assets/1e14f76b-6fd1-4f22-bf3b-66bf8c8ac4ca" />

I have more things but they are not necessary.
We will now open a Powershell and go to this directory, inside it we run the next command:

```sh
java -jar trino-cli-476-executable.jar --server https://trino.local:32622 --user xavier --catalog lakehouse --insecure --password
```
Notice how we use *https* instead of *http* because there's TLS encryption. We then put the password which is '1234' and then we are in:

<img width="1083" height="457" alt="image" src="https://github.com/user-attachments/assets/8f261478-210f-47d7-b5ae-5268dda062e9" />

We can see **both catalogs, Nessie and Icerberg REST** with the respective **aliases** we gave them on the **values.yaml** for the Trino Helm Chart, **lakehouse** and **rest**-

💡 Note: It's important to know that **Trino catalogs are composed of SCHEMAS, not DATABASES**, that's how Trino names the environment within which a set of tables exist. With that said, **it's just a question on nomenclature**. These are the same objects and if they are created in Spark, they will then be **visible, accessible, writable/readable, and totally functional** in Trino with the same name that Spark gave them and viceversa.

That's how it looks like:

<img width="1083" height="598" alt="image" src="https://github.com/user-attachments/assets/bbb94186-6d45-4c33-8884-423fdaeab335" />

Don't mind the ERROR, it's just telling us that Trino wasn't capable to format the output in a nicer way, but the **current way is fully functional**.

# Creating the Gold layer

The creation of the gold layer will be easily performed by using the following SQL statements (I'm not sure if the these chained SQL statement here can act as a script the way they are presented here piled up one after the other, I will make a copy-paste for each statement on the Trino client) inside the client above started:

```sql
CREATE OR REPLACE VIEW rest.gold.addresses AS
SELECT DISTINCT
    address_id,
    address_name,
    city,
    zip_code,
    state_region,
    country
FROM lakehouse.data.orders_silver;

CREATE OR REPLACE VIEW rest.gold.customers AS
SELECT DISTINCT
    customer_id,
    customer_name,
    email,
    cell_phone,
    address_id,
    customer_genre,
    age
FROM lakehouse.data.orders_silver
WHERE customer_id IS NOT NULL;

CREATE OR REPLACE VIEW rest.gold.suppliers AS
SELECT DISTINCT
     supplier_id,
     supplier_name,
     supplier_email,
     cell_phone,
     address_id
FROM lakehouse.data.orders_silver
WHERE supplier_id IS NOT NULL;


CREATE OR REPLACE VIEW rest.gold.products AS
SELECT
    product_id,
    product_name,
    product_description,
    type_id
FROM lakehouse.data.products_silver; -- THIS SHOULD BE THE GOOD VIEW IF THE DATA WASN'T SYNTHETIC BUT DESPCRIPTIONS ARE RANDOM AND NOT BOUND THE TYPES SO LET'S MAKE THAT INSTEAD:

CREATE OR REPLACE VIEW rest.gold.products AS
SELECT
    product_id,
    product_name,
    arbitrary(product_description) AS product_description,
    type_id
FROM lakehouse.data.products_silver
GROUP BY 
    product_id,
    product_name,
    product_description,
    type_id;

CREATE OR REPLACE VIEW rest.gold.products_types AS
SELECT DISTINCT
    type_id,
    type_name,
    type_description
FROM lakehouse.data.products_silver;-- THIS SHOULD BE THE GOOD VIEW IF THE DATA WASN'T SYNTHETIC BUT DESPCRIPTIONS ARE RANDOM AND NOT BOUND THE TYPES SO LET'S MAKE THAT INSTEAD:

CREATE OR REPLACE VIEW rest.gold.products_types AS
SELECT
    type_id,
    -- pick an arbitrary complete name (apparently, in the generation script there more than one type name for each type_id value) (first non-null one)
    arbitrary(type_name) AS type_name,
    -- pick an arbitrary description (first non-null one)
    arbitrary(type_description) AS type_description
FROM lakehouse.data.products_silver
GROUP BY
    type_id;

CREATE OR REPLACE VIEW rest.gold.orders AS
    SELECT
    order_id,
    order_date,
    is_rebate,
    CASE
        WHEN supplier_id IS NULL THEN customer_id
        ELSE supplier_id
    END AS comercial_partner  
FROM lakehouse.data.orders_silver;

CREATE OR REPLACE VIEW rest.gold.products_orders AS
    SELECT
    o.order_id,
    t.product_id,
    TRY_CAST(REPLACE(REPLACE(t.product_price, '$', ''), ',', '') AS DECIMAL(10,2)) AS product_price,
    t.product_quantity
FROM lakehouse.data.orders_silver AS o
CROSS JOIN UNNEST(o.products) AS t(product_id, product_price, product_quantity); -- THIS LAST LINE IS TRINO-SPECIFIC SQL LANGUAGE, AND THERE'S NO CARTESIAN PRODUCT BETWEEN THE o TABLE AND THE UNNESTED COLUMNS LIKE IN A CONVENTIONAL CROSS JOIN, THIS LAST LINE IS EVALUATED ROW-WISE, NAMELY, THE CROSS JOIN HAPPENS ONCE FOR EVERY SINGLE ROW OF o TABLE AND THE UNNESTED FIELDS OF t PRESENT ON THE o TABLE'S ROW, SO THE REST OF THE ROWS ARE OUT OF THE SCOPE OF THE CROSS JOIN. THEREFORE, THE CARTESIAN PRODUCT TAKES PLACE BUT ONLY BETWEEN A SINGLE ROW ON THE LEFT SIDE AND ANOTHER SINGLE ROW WITH THE UNNESTED FIELDS OF THIS SAME ROW ON THE OTHER SIDE OF THE JOIN WITH SIMPLY RETURNS A SIGLE COMBINATION OF THESE TWO ROWS MATCHED. 
```
That would conclude the Golden layer and we can now start talking about the connection between Trino and Power BI.

# Connecting Trino to Power BI

There are several specific ways to connect Trino to Power BI but all of them can fall into one of these two categories, either we use a **custom connector** or we use an **ODBC/JDBC driver** for Trino and then use it to create a **DSN (Data Source Names)**, then we can use it inside the generic out-of-the-box ODBC connector Power BI comes with. We will use the **custom connector** approach, it is simpler and more direct.  

The specific custom connector we will use us the one from https://github.com/CreativeDataEU/PowerBITrinoConnector, many thanks to its creator pichlerpa. The implementation is very easy and simple to use, we only need to put the **.mez** inside the folder for custom connectors of Power BI which, in my case has the path **C:\Users\Usuario\Documents\Power BI Desktop\Custom Connectors**:

<img width="1273" height="564" alt="image" src="https://github.com/user-attachments/assets/69797d22-0b91-4666-aa59-b5807c5edec4" />

Once there we can start Power BI and it should automatically detect the custom connector and add it to the list inside the Get Data window:

<img width="1918" height="1012" alt="image" src="https://github.com/user-attachments/assets/6cf5a38c-57f1-4c1f-9dfa-19346bad02f8" />

Then we just have to enter the information about our trino server so the connector can detect it:

<img width="1919" height="1011" alt="image" src="https://github.com/user-attachments/assets/f1a17f88-f8f3-4113-b514-8bfbfeee8e57" />

And we will have everything well set when we see the Power BI explorer querying our trino server:

<img width="1919" height="1020" alt="image" src="https://github.com/user-attachments/assets/706a8eea-4fc0-4275-b335-af6d2117d5fb" />

Finally, we can import all tables and add the relationships between the tables to properly shape the sematic model:

<img width="1919" height="1012" alt="image" src="https://github.com/user-attachments/assets/2a000129-7ee2-4b80-b7f1-d2b24af090e5" />

And finally we can fill the first page of the report with diiferent Power BI **visuals** to take a look at the data.

💡 Note: Since the data is synthetic there's not much to see and results will be fostered by the randomnes of the Python scripts used to generate the data on MongoDB. So there's not much interest on diving deep into the data. Nevertheless, let's see a good starting point to explore this data.

In this report page there a number of things made that help us represent out data better, these consist of:

- A **geographical hierarchy**, namely, cities, inside, regions inside inside countries. Used in the treemap.
  
- An **inactive relationship** between 'gold orders' and 'gold suppliers' (Power BI **semantic models** have this limitation to know how to **spread filters across visuals consistently**, we will work only with customers orders). And the subsecuent **blank values** we will have on the treemap that come from all the **quantities on orders_products that come from suppliers instead of customers**. We can see that they are the exact same number of purchases that has no sex because supplier firms have no sex attribute on the second image, we will then just **exclude** them on the page.

  <img width="1919" height="1014" alt="image" src="https://github.com/user-attachments/assets/9a734f57-c596-4349-a7b7-9b7086f1547f" />

  <img width="1328" height="742" alt="image" src="https://github.com/user-attachments/assets/dc8f43e9-8154-4847-b5bd-029b0bad91df" />

  <img width="1474" height="982" alt="image" src="https://github.com/user-attachments/assets/757abbc6-33b8-496c-bb98-18591c9c4651" />

- **Cross-highlighting** occurs from the donut chart and bar chart to the rest of the charts and **cross-filtering** from the treemap also to the rest of the charts when we **drill down** into a specific country/region/city (that is why we use a hierarchy) when is enabled by clicking on it or **cross-highlighting if the drill mode is disabled**. Some examples are selecting men and seeing that they are more relatively present on France and the United States on number of purchases but not in Spain, where women buy more relative to men as we see on the first image, or we could click on Spain with Drill mode enabled as see how Sports products become the most pricey among them all behind furniture products which are otherwise the most expensive outside the specific spanish market.

  <img width="1317" height="734" alt="image" src="https://github.com/user-attachments/assets/b54d5174-91d0-4fa0-b7dd-6701cbaa1b22" />

  
  <img width="1312" height="734" alt="image" src="https://github.com/user-attachments/assets/9da48afd-f086-44fd-8655-fb8d99139a44" />


💡 Note: If you know Power BI you have perhaps noticed that the connector I used **only supports Import mode** on Power BI, that means that all data is **moved from Trino to Power BI** and then Power BI works with the data snapshot it imported disregarding the near real time changes. **¿So where is the near real time I claim?** The truth is that **DirectQuery** mode is perfectly achivable from Trino to Power BI, in fact, Trino is one of the best options to feed BI visualizations in real time. It was just that I didn't found a free way of doing it, but if we are **in a production environment** I've found, at least, **two ways accomplishing it**, we can choose between the managed version of **Trino from Starburst** that has an **out-of-the-box dedicated Power BI connector** on Power BI desktop and the Service I think, or we could use the **ODBC driver from Simba + their custom connector for Trino** that also allows **Direct Query**. But just demonstration purposes a Import mode works just fine.  


# Disclaimer

Some security measures have been intentionally simplified or omitted.

This is because, while security is essential in production environments and I've leveraged the opportunity to learn about TLS encryption, a general robust approach to security would add unnecessary complexity without much educational value for this project’s main goal: **integration.** Besides, security is usually environment specific, for instance:

Kubernetes uses **RBAC rules**.

AWS uses **IAM roles** and **Security Groups**.

Power BI and Microsoft Fabric/Azure use **Microsoft Entra ID** within **Microsoft 365** environments.

And we could continue with Databricks, Snowflake, etc...

