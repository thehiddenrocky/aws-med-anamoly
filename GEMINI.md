# Gemini CLI Instruction Guide: Medicare Fraud Detection Data Pipeline

Welcome to the Medicare Fraud Detection project context page. This workspace contains an end-to-end AWS data engineering and machine learning pipeline that ingests public CMS Medicare claims data, processes it via PySpark (locally or via AWS Glue), performs analytics using Amazon Athena, and trains an anomaly detection model on Amazon SageMaker to flag fraudulent providers.

---

## 🏗️ System Architecture & Data Flow

The project processes datasets through a classic "Medallion Architecture" combined with downstream serverless analytics and machine learning:

1. **Raw Zone (Bronze)**: Original CMS public CSV datasets uploaded to Amazon S3 (`s3://medicare-fraud-raw-<account_id>/`).
2. **Processed Zone (Silver)**: PySpark ETL cleaned, structured, and feature-engineered data stored as highly optimized Parquet files partitioned by `Year` and `State` in S3 (`s3://medicare-fraud-processed-<account_id>/`).
3. **AWS Glue Data Catalog & Crawler**: Registers schemas and metadata of the processed silver datasets.
4. **Amazon Athena**: Serverless SQL analytics queries over the silver-tier parquet datasets.
5. **Analytics Zone (Gold)**: Aggregated provider features ready for machine learning.
6. **Amazon SageMaker**: Training an Isolation Forest anomaly detection model to assign risk scores to NPI providers.
7. **Serving Layer**: AWS Lambda + API Gateway delivering real-time REST API risk assessment.
8. **Visualization**: Amazon QuickSight providing interactive dashboards for flagged anomalies, heatmaps, and metrics.

---

## 📁 Directory Structure

The repository is organized as follows:

```text
/
├── .gitignore                   # Standard ignore file (ignores DS_Store, venv, processed outputs)
├── glue-s3-policy.json          # Least-privilege IAM policy for Glue S3 read/write
├── glue-trust-policy.json       # IAM trust policy allowing the Glue service to assume roles
├── datasets/
│   ├── kaggle-med-fraud-detection/
│   │   ├── Train_Beneficiarydata-*.csv # CMS beneficiary profile details
│   │   ├── Train_Inpatientdata-*.csv   # Inpatient hospital claims
│   │   ├── Train_Outpatientdata-*.csv  # Outpatient claims
│   │   └── ... (Test files and other raw CSVs)
│   └── samples/
│       ├── train_inpatient_sample.csv  # Sliced small subset of inpatient data for local runs
│       └── processed_parquet/          # Local output directory for partitioned Parquet datasets (ignored by git)
├── docs/
│   ├── aws-glue-partition-guide.md    # In-depth guide on partitioning strategies, coalescing, and S3 paths
│   └── notes-on-partitioning.txt       # Technical decision guide (heuristics, cardinality limits, size thresholds)
├── etl-scripts/
│   └── medicare_etl.py          # Dual-environment PySpark script (runs locally or inside AWS Glue ETL)
├── plans/
│   ├── aws-glue-setup.md        # Comprehensive instructions for deploying policies, scripts, and Glue jobs via CLI
│   └── general-plan.md          # Multi-week roadmap, Athena query specs, ML models, and downstream API handlers
└── venv/                        # Local Python virtual environment
```

---

## 🛠️ Building & Running Locally

### 1. Prerequisite Environment Setup
The PySpark script requires **Java (OpenJDK 17 or OpenJDK 25)** and a standard Python environment.

* **Install Java on macOS (via Homebrew):**
  ```bash
  brew install openjdk@17
  ```
  *(The PySpark script `medicare_etl.py` contains auto-detection logic to automatically locate Homebrew-installed Java runtimes and inject `JAVA_HOME` variables on startup).*

* **Install Python Dependencies:**
  ```bash
  # Activate the virtual environment
  source venv/bin/activate

  # Install required libraries (PySpark, etc.)
  pip install pyspark
  ```

### 2. Running PySpark Locally
To run the ETL job on your local machine using sample slices:
```bash
python etl-scripts/medicare_etl.py
```
* **Local Behavior:** The script detects the absence of AWS Glue environments, constructs a standard local Spark Session (`master("local[*]")`), loads data from `datasets/samples/train_inpatient_sample.csv` joined with `datasets/kaggle-med-fraud-detection/Train_Beneficiarydata-1542865627584.csv`, extracts the partitioned keys, and outputs a nested directory to `datasets/samples/processed_parquet/`.

### 3. Verifying Local Output Integrity
The script automatically validates the local output file. You can also manually review the partitioned structures:
```bash
ls -R datasets/samples/processed_parquet/
```
Output folders will be organized in Hive-style format: `Year=2008/State=01/part-00000-*.parquet`.

---

## ☁️ Running on AWS Glue ETL

To deploy the data pipeline onto serverless AWS Spark infrastructure:

### 1. Set Up IAM Execution Role
Initialize the required service roles:
```bash
# 1. Create the role with AWS Glue trust policy
aws iam create-role \
    --role-name MedicareGlueETLRole \
    --assume-role-policy-document file://glue-trust-policy.json

# 2. Attach standard AWSGlueServiceRole policy
aws iam attach-role-policy \
    --role-name MedicareGlueETLRole \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole

# 3. Apply custom least-privilege S3 bucket access policy
aws iam put-role-policy \
    --role-name MedicareGlueETLRole \
    --policy-name S3AccessForMedicareETL \
    --policy-document file://glue-s3-policy.json
```

### 2. Upload ETL Code to S3 Raw Bucket
```bash
aws s3 cp etl-scripts/medicare_etl.py s3://medicare-fraud-raw-023413058557/scripts/medicare_etl.py
```

### 3. Create and Register the Glue Job
```bash
aws glue create-job \
    --name medicare-inpatient-etl \
    --role MedicareGlueETLRole \
    --command '{"Name": "glueetl", "ScriptLocation": "s3://medicare-fraud-raw-023413058557/scripts/medicare_etl.py", "PythonVersion": "3"}' \
    --glue-version "4.0"
```

### 4. Trigger and Monitor Execution
```bash
# Trigger the job asynchronously
aws glue start-job-run --job-name medicare-inpatient-etl

# Poll status using the returned JobRunId
aws glue get-job-run \
    --job-name medicare-inpatient-etl \
    --run-id <INSERT_YOUR_JOB_RUN_ID> \
    --query "JobRun.JobRunState" \
    --output text
```

---

## 📐 Development Conventions & Real-world Heuristics

Follow these structural guidelines when extending or modifying this workspace:

1. **Dual-Environment Compatibility**:
   * All PySpark ETL scripts **must** feature environment auto-detection using `try/except` for `awsglue` library imports.
   * Maintain fallback local paths pointing to sample datasets so offline local testing remains fully supported without cloud resources.

2. **The "Small File Problem" & Optimization Heuristics**:
   * Small datasets (< 10 GB) should theoretically remain flat in production to avoid S3 metadata and request bottlenecks.
   * When practicing/learning nested partitioning strategies (e.g., `Year` / `State`), **always use `.coalesce(1)`** immediately prior to writing to force Spark to output exactly *one consolidated Parquet file* inside each partition folder, preventing thousands of 8 KB shards.
   * Never partition on high-cardinality fields such as `Provider NPI` or `ClaimID`. Treat high-cardinality values by sorting internally within flat Parquet files to utilize Parquet footer indexing instead of directory partitioning.

3. **Schema Integrity & Joins**:
   * Perform left joins optimized for memory. Only select the minimum required columns from secondary lookup datasets (e.g., only `BeneID` and `State` from the Beneficiary file) before joining them with massive transactional tables.
   * Write columns in snake_case format or keep original source casings intact depending on downstream catalog structures.
