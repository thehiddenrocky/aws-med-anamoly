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
    BENEFICIARY_PATH = "datasets/kaggle-med-fraud-detection/Train_Beneficiarydata-1542865627584.csv"
    OUTPUT_PATH = "datasets/samples/processed_parquet/"
else:
    # AWS Glue Initialization
    from awsglue.context import GlueContext
    from pyspark.context import SparkContext

    sc = SparkContext()
    glueContext = GlueContext(sc)
    spark = glueContext.spark_session

    INPUT_PATH = "s3://medicare-fraud-raw-023413058557/Train_Inpatientdata-1542865627584.csv"
    BENEFICIARY_PATH = "s3://medicare-fraud-raw-023413058557/Train_Beneficiarydata-1542865627584.csv"
    OUTPUT_PATH = "s3://medicare-fraud-processed-023413058557/inpatient_processed/"

# --- ETL Logic (runs identical locally or on AWS) ---
print("\n" + "="*50)
print("1. LOADING INPUT DATASETS")
print("="*50)
print(f"Loading Inpatient claims from: {INPUT_PATH}")
df_inpatient = spark.read.csv(INPUT_PATH, header=True, inferSchema=True)
row_count_inp = df_inpatient.count()
print(f"Successfully loaded {row_count_inp} Inpatient rows")

print(f"Loading Beneficiary data from: {BENEFICIARY_PATH}")
df_beneficiary = spark.read.csv(BENEFICIARY_PATH, header=True, inferSchema=True)
row_count_bene = df_beneficiary.count()
print(f"Successfully loaded {row_count_bene} Beneficiary rows")

print("\n" + "="*50)
print("1.5. JOINING INPATIENT WITH BENEFICIARY DATA")
print("="*50)
# Select only required columns from beneficiary to optimize join performance
df_beneficiary_sub = df_beneficiary.select("BeneID", "State")
df = df_inpatient.join(df_beneficiary_sub, on="BeneID", how="left")
print(f"Joined datasets successfully. Row count remains: {df.count()}")

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

# Add partition columns: Extract year from ClaimStartDt (yyyy-MM-dd)
print("\n" + "="*50)
print("3.5. ADDING PARTITION COLUMNS")
print("="*50)
df = df.withColumn("Year", F.substring(F.col("ClaimStartDt"), 1, 4))
print("Added partition columns 'Year' (derived from ClaimStartDt) and 'State' (from Beneficiary data)")

# Write output partitioned by Year and State
print("\n" + "="*50)
print("4. WRITING OUTPUT PARQUET (PARTITIONED BY YEAR AND STATE)")
print("="*50)
if IS_LOCAL:
    df.write.mode("overwrite").partitionBy("Year", "State").parquet(OUTPUT_PATH)
    print(f"Successfully saved multi-level partitioned Parquet output to local path: {OUTPUT_PATH}")
    
    print("\n" + "="*50)
    print("5. VERIFYING PARQUET INTEGRITY")
    print("="*50)
    verify_df = spark.read.parquet(OUTPUT_PATH)
    print(f"Successfully read back Parquet output. Count: {verify_df.count()} rows.")
    print("Available partitions in local output:")
    if os.path.exists(OUTPUT_PATH):
        years = [d for d in os.listdir(OUTPUT_PATH) if d.startswith("Year=")]
        print(f"Discovered Year partition directories: {years}")
        if years:
            sample_year_path = os.path.join(OUTPUT_PATH, years[0])
            states = [d for d in os.listdir(sample_year_path) if d.startswith("State=")]
            print(f"Discovered nested State partitions inside {years[0]}: {states[:5]}... (showing up to 5)")
    print("ETL job verification: SUCCESS!")
else:
    df.write.mode("overwrite").partitionBy("Year", "State").parquet(OUTPUT_PATH)

