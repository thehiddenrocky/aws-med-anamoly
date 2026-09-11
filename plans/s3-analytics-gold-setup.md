# AWS S3 Analytics Zone (Gold Tier): ML Feature Store Setup Plan

This plan outlines the design, configuration, and implementation steps to construct the **Gold Tier / Analytics Zone** within our Medicare Fraud Detection data pipeline. 

By utilizing **Amazon Athena** and its serverless **Create Table As Select (CTAS)** pattern, we will aggregate raw claim-level transaction data from the **Silver Tier** (`medicare_db.inpatient_processed`), apply our advanced forensic healthcare features, and output highly optimized, machine-learning-ready Parquet datasets straight to the Gold S3 prefix (`s3://medicare-fraud-analytics-023413058557/features/`).

---

## 1. Architectural Overview & Data Flow

The Gold Tier is designed as an analytical, provider-centric feature store. While the Silver Tier maintains granular transaction (claim-level) partitioning, the Gold Tier consolidates features at the provider (`NPI`) level, which is the exact entity that downstream machine learning models (e.g., SageMaker Isolation Forest) will evaluate for anomalies.

```text
  Silver S3 Layer: s3://medicare-fraud-processed-023413058557/inpatient_processed/
                                     ↓
                     Amazon Athena Serverless Engine
    (Executes CTAS Query to Aggregate and Calculate Forensic Metrics)
                                     ↓
  Gold S3 Layer: s3://medicare-fraud-analytics-023413058557/features/
                (Consolidated Parquet Files - Ready for SageMaker)
```

### Storage Design & Partitioning Strategy
- **Bucket Location**: `s3://medicare-fraud-analytics-023413058557/features/`
- **Output Format**: Columnar Parquet (`format = 'PARQUET'`).
- **Partitioning**: Since the resulting dataset contains unique aggregated provider profiles (~thousands of rows rather than millions of raw claims), we **will not partition** this gold-tier table. This avoids the "small file problem" in S3 and allows downstream Pandas/PySpark ML pipelines to perform highly efficient single-prefix loads without directory-traversal overhead.

---

## 2. Feature Schema & Forensic Rationale

The feature store will contain **11 key features** meticulously engineered to expose medical billing abnormalities, based on historical forensic auditing findings:

| Feature Name | Athena SQL Data Type | Forensic Interpretation & ML Signal |
| :--- | :--- | :--- |
| **`provider`** | `VARCHAR` | Unique ID of the healthcare facility / provider (Entity Key). |
| **`total_claims`** | `BIGINT` | Total inpatient claims submitted. Measures provider volume. |
| **`unique_patients`** | `BIGINT` | Total distinct beneficiaries. Helps detect patient concentration. |
| **`total_reimbursement`** | `BIGINT` | Gross financial payout. Detects high-dollar absolute outliers. |
| **`avg_reimbursed_per_claim`** | `DOUBLE` | Average payout size. Detects consistent billing upcoding. |
| **`avg_deductible_paid`** | `DOUBLE` | Average patient deductible share. Highlights high-cost procedures. |
| **`avg_length_of_stay`** | `DOUBLE` | Average inpatient stay (days). Normalizes patient severity. |
| **`avg_daily_reimbursement`** | `DOUBLE` | Average reimbursement per day. High daily rates suggest hyper-inflation. |
| **`max_daily_reimbursement`** | `DOUBLE` | Maximum daily rate. Pinpoints extreme single-stay financial spikes. |
| **`claims_per_patient_ratio`** | `DOUBLE` | Volume of claims divided by unique patients. High ratio indicates churn. |
| **`high_daily_claims_ratio`** | `DOUBLE` | Percentage of provider's claims where daily rate exceeds **$10,000**. |

---

## 3. Athena CTAS Creation Script

To generate this feature store table programmatically, we will run the following optimized Athena CTAS query. This query handles:
1. **Division-by-Zero Protection**: Adds a `1.0` day buffer to same-day admits/discharges (`length_of_stay = 0`) to accurately compute the Daily Reimbursement Rate.
2. **Schema Alignment**: Handles the string-to-double casting of monetary metrics (such as deductibles).
3. **Engineered Metrics**: Calculates patient-to-claim churn ratio and high-cost claim ratios.

```sql
CREATE TABLE medicare_db.provider_ml_features
WITH (
     format = 'PARQUET',
     external_location = 's3://medicare-fraud-analytics-023413058557/features/'
) AS 
WITH claim_level_features AS (
    SELECT 
        provider,
        claimid,
        beneid,
        inscclaimamtreimbursed,
        try_cast(deductibleamtpaid AS DOUBLE) AS deductible_paid,
        date_diff('day', admissiondt, dischargedt) AS length_of_stay,
        (CAST(inscclaimamtreimbursed AS DOUBLE) / (date_diff('day', admissiondt, dischargedt) + 1.0)) AS daily_reimbursement_rate
    FROM medicare_db.inpatient_processed
),
provider_aggregates AS (
    SELECT 
        provider,
        COUNT(DISTINCT claimid) AS total_claims,
        COUNT(DISTINCT beneid) AS unique_patients,
        SUM(inscclaimamtreimbursed) AS total_reimbursement,
        AVG(inscclaimamtreimbursed) AS avg_reimbursed_per_claim,
        AVG(deductible_paid) AS avg_deductible_paid,
        AVG(length_of_stay) AS avg_length_of_stay,
        AVG(daily_reimbursement_rate) AS avg_daily_reimbursement,
        MAX(daily_reimbursement_rate) AS max_daily_reimbursement,
        SUM(CASE WHEN daily_reimbursement_rate > 10000.0 THEN 1 ELSE 0 END) AS high_daily_claims_count
    FROM claim_level_features
    GROUP BY provider
)
SELECT 
    provider,
    total_claims,
    unique_patients,
    total_reimbursement,
    avg_reimbursed_per_claim,
    avg_deductible_paid,
    avg_length_of_stay,
    ROUND(avg_daily_reimbursement, 2) AS avg_daily_reimbursement,
    ROUND(max_daily_reimbursement, 2) AS max_daily_reimbursement,
    ROUND(CAST(total_claims AS DOUBLE) / unique_patients, 4) AS claims_per_patient_ratio,
    ROUND(CAST(high_daily_claims_count AS DOUBLE) / total_claims, 4) AS high_daily_claims_ratio
FROM provider_aggregates;
```

---

## 4. Verification and Data Integrity Queries

Once the CTAS query runs, we must verify the schema and output files to confirm correct formatting before feeding them to Amazon SageMaker.

### Query A: Verify Table Metadata and Structure
Verify the resulting columns match our target feature specification exactly:
```sql
DESCRIBE medicare_db.provider_ml_features;
```

### Query B: Total Row Count & Basic Null Audit
Ensure the dataset contains no incomplete records for critical features:
```sql
SELECT 
    COUNT(*) AS total_providers,
    COUNT(CASE WHEN provider IS NULL THEN 1 END) AS null_providers,
    COUNT(CASE WHEN avg_daily_reimbursement IS NULL THEN 1 END) AS null_avg_daily,
    COUNT(CASE WHEN claims_per_patient_ratio IS NULL THEN 1 END) AS null_patient_ratio
FROM medicare_db.provider_ml_features;
```

### Query C: Forensic Cross-Check (Reproduce Known Anomalies)
Confirm that our engineered feature set successfully exposes the top suspicious providers identified during forensic exploration (e.g., `PRV53033`, `PRV52627`):
```sql
SELECT 
    provider,
    total_claims,
    unique_patients,
    avg_length_of_stay,
    avg_daily_reimbursement,
    max_daily_reimbursement,
    high_daily_claims_ratio,
    claims_per_patient_ratio
FROM medicare_db.provider_ml_features
WHERE provider IN ('PRV53033', 'PRV52627', 'PRV53471')
ORDER BY avg_daily_reimbursement DESC;
```

---

## 5. Downstream Machine Learning Integration

Downstream ML pipelines (running in Amazon SageMaker or local python environments) can directly load this Gold layer table using standard cloud-native data science libraries:

### Option A: Loading directly via Pandas / S3FS (Fastest for SageMaker Notebooks)
```python
import pandas as pd
import s3fs

# Read the curated gold-tier feature store
df = pd.read_parquet('s3://medicare-fraud-analytics-023413058557/features/')

print(f"Successfully loaded {len(df)} provider profiles with features:")
print(df.columns.tolist())
```

### Option B: Training Isolation Forest on Gold Features
The Isolation Forest model will ingest these normalized features:
```python
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

# Select engineered features
ml_features = [
    'total_claims', 
    'unique_patients', 
    'avg_length_of_stay', 
    'avg_daily_reimbursement', 
    'max_daily_reimbursement', 
    'claims_per_patient_ratio', 
    'high_daily_claims_ratio'
]

X = df[ml_features].fillna(0)

# Scale
scaler = StandardScaler()
X_scaled = scaler.fit_transform(X)

# Fit Isolation Forest
model = IsolationForest(contamination=0.05, random_state=42)
df['is_anomaly'] = model.fit_predict(X_scaled)
```

---

## 6. Execution Instructions (Once Authorized)

To execute this plan, run the following steps:

1. **Clean Existing Directory (If re-running)**:
   ```bash
   aws s3 rm s3://medicare-fraud-analytics-023413058557/features/ --recursive
   ```
2. **Execute CTAS Query via AWS CLI**:
   ```bash
   aws athena start-query-execution \
       --query-string "CREATE TABLE medicare_db.provider_ml_features WITH (format = 'PARQUET', external_location = 's3://medicare-fraud-analytics-023413058557/features/') AS ..." \
       --query-execution-context Database=medicare_db \
       --work-group primary
   ```
3. **Verify S3 Object Output**:
   ```bash
   aws s3 ls s3://medicare-fraud-analytics-023413058557/features/
   ```
   *(Ensure files with `.parquet` extensions are written under the prefix and that multiple, consolidated partitions are not created, preserving a clean structure for the training job).*
