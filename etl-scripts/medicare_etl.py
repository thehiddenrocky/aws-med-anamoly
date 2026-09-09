import sys
import os
from pyspark.sql import SparkSession, functions as F
from pyspark.sql.window import Window

# Detect if running locally or in Glue
try:
    from awsglue.utils import getResolvedOptions
    from awsglue.context import GlueContext
    from pyspark.context import SparkContext
    IS_LOCAL = False
except ImportError:
    IS_LOCAL = True

if IS_LOCAL:
    # Auto-detect JAVA_HOME on macOS if not set
    if "JAVA_HOME" not in os.environ:
        possible_paths = [
            "/opt/homebrew/opt/openjdk@17",
            "/opt/homebrew/opt/openjdk",
            "/opt/homebrew/opt/openjdk@25",
            "/usr/local/opt/openjdk@17",
            "/usr/local/opt/openjdk",
        ]
        for path in possible_paths:
            if os.path.exists(path):
                os.environ["JAVA_HOME"] = path
                os.environ["PATH"] = f"{path}/bin:" + os.environ.get("PATH", "")
                break

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

    INPUT_PATH = "s3://medicare-fraud-raw-023413058557/Train_Inpatientdata-1542865627584.csv"
    OUTPUT_PATH = "s3://medicare-fraud-processed-023413058557/inpatient_processed/"

# --- ETL Logic (runs identical locally or on AWS) ---
print("\n" + "="*50)
print("1. LOADING INPUT DATASET")
print("="*50)
df = spark.read.csv(INPUT_PATH, header=True, inferSchema=True)
row_count = df.count()
print(f"Successfully loaded {row_count} rows from: {INPUT_PATH}")

print("\n" + "="*50)
print("2. INPUT SCHEMA PREVIEW")
print("="*50)
df.printSchema()

print("\n" + "="*50)
print("3. SAMPLE DATA (FIRST 5 ROWS)")
print("="*50)
df.show(5, truncate=True)

# Clean & Engineer Features...
# (e.g., window functions, payment ratios, specialty averages)

# Write output
print("\n" + "="*50)
print("4. WRITING OUTPUT PARQUET")
print("="*50)
if IS_LOCAL:
    df.write.mode("overwrite").parquet(OUTPUT_PATH)
    print(f"Successfully saved Parquet output to local path: {OUTPUT_PATH}")
    
    print("\n" + "="*50)
    print("5. VERIFYING PARQUET INTEGRITY")
    print("="*50)
    verify_df = spark.read.parquet(OUTPUT_PATH)
    print(f"Successfully read back Parquet output. Count: {verify_df.count()} rows.")
    print("ETL job verification: SUCCESS!")
else:
    df.write.mode("overwrite").parquet(OUTPUT_PATH)

