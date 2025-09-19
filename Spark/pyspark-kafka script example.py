from pyspark.sql.functions import to_json, struct, col, expr, row_number, from_json
from pyspark.sql.window import Window
from pyspark.sql.types import StructType, StringType, IntegerType

data = [("Alice", 34), ("Bob", 45), ("Charlie", 29)]
columns = ["name", "age"]
df = spark.createDataFrame(data, columns)
df.show()

streaming_input_df = spark.readStream.format("rate").option("rowPerSecond", 1).load()
windowSpec = Window.orderBy("name")
df_indexed = df.withColumn("idx", row_number().over(windowSpec) - 1)
n = df_indexed.count()
streaming_df = streaming_input_df.withColumn("idx", (col("value") % n).cast("int"))
streaming_data = streaming_df.join(df_indexed, "idx").select("name", "age")

kafka_df = streaming_data.selectExpr(
    "CAST(name AS STRING) AS key",
    "to_json(struct(*)) AS value"
)

query = kafka_df.writeStream.format("kafka") \
    .option("kafka.bootstrap.servers", "my-cluster-kafka-bootstrap.kafka:9092") \
    .option("topic", "my_topic") \
    .option("checkpointLocation", "/tmp/spark-kafka-checkpoint") \
    .start()

query.awaitTermination()
query.stop()

# Definir el esquema del JSON que tú envías (ajusta si es necesario)
schema = StructType() \
    .add("name", StringType()) \
    .add("age", IntegerType())

# Leer del tópico Kafka
df = spark.readStream.format("kafka") \
    .option("kafka.bootstrap.servers", "my-cluster-kafka-bootstrap.kafka:9092") \
    .option("subscribe", "my_topic") \
    .option("startingOffsets", "earliest") \
    .load()

# Los datos vienen en key y value como binarios, hay que convertirlos a string
df_parsed = df.selectExpr("CAST(key AS STRING)", "CAST(value AS STRING)")

# Si el value es JSON, lo convertimos a columnas estructuradas
df_json = df_parsed.withColumn("data", from_json(col("value"), schema)).select("data.*")

# Mostrar en consola (modo append o update según tu lógica)
query = df_json.writeStream \
    .format("console") \
    .option("checkpointLocation", "/tmp/kafka-read-checkpoint") \
    .outputMode("append") \
    .start()

query.awaitTermination()
