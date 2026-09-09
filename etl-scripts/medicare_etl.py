import sys
import os
from pyspark.sql import SparkSession, functions as F
from pyspark.sql.window import Window

# Detect if running locally or in Glue
IS_LOCAL = "GLUE_INSTALLATION_DIR" not in os.environ and "AWS_GLUE_HOME" not in
os.environ

if IS_LOCAL:
    # Local PySpark Initialization
    spark = SparkSession.builder \
        .appName("MedicareETL-Local") \
        .master("local[*]") \
        .getOrCreate()

    # Local sample paths
    INPUT_PATH = "datasets/samples/train_inpatient_sample.csv"
    OUTPUT_PATH = "datasets/samples/processed_parquet/"
else:
    # AWS Glue Initialization
    from awsglue.context import GlueContext
    from pyspark.context import SparkContext

    sc = SparkContext()
    glueContext = GlueContext(sc)
    spark = glueContext.spark_session

    INPUT_PATH = "s3://medicare-fraud-raw/part_b/2022/"
    OUTPUT_PATH = "s3://medicare-fraud-processed/part_b/2022/"

# --- ETL Logic (runs identical locally or on AWS) ---
df = spark.read.csv(INPUT_PATH, header=True, inferSchema=True)

# Clean & Engineer Features...
# (e.g., window functions, payment ratios, specialty averages)

# Write output
if IS_LOCAL:
    df.write.mode("overwrite").parquet(OUTPUT_PATH)
else:
    df.write.mode("overwrite").partitionBy("provider_type").parquet(OUTPUT_PATH)
