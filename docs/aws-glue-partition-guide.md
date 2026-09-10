# AWS Glue Partitioning Guide: Step-by-Step

This guide provides a detailed, step-by-step walkthrough of the multi-level partitioning strategy implemented for the Medicare Inpatient Claims dataset using AWS Glue and PySpark. It explains what changes were made, why they were made, and how to execute the data pipeline on AWS.

---

## Background & Partitioning Strategy

### Why Partitioning Matters
Partitioning reduces query costs and execution time in Amazon Athena by enabling **partition pruning**. When queries include filters on partition keys (e.g., `WHERE Year = '2008' AND State = '39'`), Athena only scans the S3 prefixes matching those partitions, avoiding a full table scan.
* **Athena Billing Pricing Model:** $5.00 per Terabyte (TB) of data scanned. By restricting scans, we minimize operational costs.
* **Avoid High Cardinality:** We avoid partitioning on high-cardinality fields like `Provider` or `ClaimID` because it leads to the **"small file problem"** (millions of tiny files which degrades performance on Hadoop/S3).
* **Target File Size:** In production, we target individual Parquet file sizes between 128 MB and 1 GB.

### Selected Partition Keys
We selected a two-level nested partitioning strategy:
1. **`Year`**: Derived dynamically from the `ClaimStartDt` field.
2. **`State`**: Derived from the numeric state code in the Beneficiary dataset.

> **Note on Data Limitations:** The raw Inpatient Claims dataset does not natively contain State information. To partition by State, we perform an optimized left join with the Beneficiary dataset.

---

## Step-by-Step Implementation

### Step 1: Modify the ETL Script (`etl-scripts/medicare_etl.py`)
To enable multi-level partitioning, the PySpark script was updated with the following core improvements:

1. **Optimized Left Join for State Information**:
   We joined the Inpatient Claims dataset with the Beneficiary dataset using `BeneID` as the joining key. To optimize memory usage during the join, we selected only the required columns from the Beneficiary dataframe.
   ```python
   # Select only required columns from beneficiary to optimize join performance
   df_beneficiary_sub = df_beneficiary.select("BeneID", "State")
   df = df_inpatient.join(df_beneficiary_sub, on="BeneID", how="left")
   ```

2. **Extract Year Partition Column**:
   The `ClaimStartDt` values are formatted as `YYYY-MM-DD`. We used PySpark's substring function to safely extract the 4-digit year as a partition column:
   ```python
   df = df.withColumn("Year", F.substring(F.col("ClaimStartDt"), 1, 4))
   ```

3. **Multi-Level Partition Writing**:
   We outputted the final dataframe using the `.partitionBy("Year", "State")` modifier. This outputs a Hive-style layout in S3 (e.g., `Year=2008/State=39/`).
   ```python
   df.write.mode("overwrite").partitionBy("Year", "State").parquet(OUTPUT_PATH)
   ```

---

### Step 2: Upload the Script to S3
The local Python script must be copied to your raw S3 bucket so that the AWS Glue service can access and execute it.

**Command:**
```bash
aws s3 cp etl-scripts/medicare_etl.py s3://medicare-fraud-raw-023413058557/scripts/medicare_etl.py
```

---

### Step 3: Create the AWS Glue Job
Create the Spark ETL job in AWS Glue using the `aws glue create-job` CLI command.

> **CRITICAL IAM ROLE FIX:** When creating the job, we must use the correct, pre-existing IAM execution role: **`MedicareGlueETLRole`** (which has the necessary `glue.amazonaws.com` Trust Policy configured) instead of any temporary or non-existent service roles.

**Command:**
```bash
aws glue create-job \
    --name medicare-inpatient-etl \
    --role MedicareGlueETLRole \
    --command '{"Name": "glueetl", "ScriptLocation": "s3://medicare-fraud-raw-023413058557/scripts/medicare_etl.py", "PythonVersion": "3"}' \
    --glue-version "4.0"
```

---

### Step 4: Run the AWS Glue Job
Trigger the execution of the Glue ETL job. AWS Glue will spin up an on-demand Spark cluster to run the ETL script and write the partitioned Parquet files.

**Command:**
```bash
aws glue start-job-run --job-name medicare-inpatient-etl
```

---

### Step 5: Monitor the Job Run
Since AWS Glue runs asynchronously in the background, you can poll and monitor the state of the execution using the `JobRunId` returned by Step 4.

**Command:**
```bash
aws glue get-job-run \
    --job-name medicare-inpatient-etl \
    --run-id <INSERT_YOUR_JOB_RUN_ID> \
    --query "JobRun.JobRunState" \
    --output text
```

* **Possible States:** `STARTING` ➡️ `RUNNING` ➡️ `SUCCEEDED` / `FAILED`.

---

## Resulting Hive-Style Folder Structure in S3

Once the Glue job runs successfully, your processed bucket (`s3://medicare-fraud-processed-023413058557/inpatient_processed/`) will automatically organize the output into a clean, hierarchical structure:

```text
inpatient_processed/
├── Year=2008/
│   ├── State=01/
│   │   └── part-00000-...parquet
│   ├── State=02/
│   │   └── part-00000-...parquet
│   └── ...
├── Year=2009/
│   ├── State=01/
│   └── ...
└── ...
```

This nested structure allows Amazon Athena and AWS Glue Crawlers to register partitions perfectly, optimizing query performance and costs down the road!
