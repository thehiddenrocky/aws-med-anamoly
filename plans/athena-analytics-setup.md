# Amazon Athena Analytics Setup Plan

With the AWS Glue Data Catalog setup complete and the crawler in a `READY` state, our processed Inpatient Medicare Claims dataset is successfully mapped as the `inpatient_processed` table under the `medicare_db` database. 

This plan outlines the next steps to configure Amazon Athena and execute optimized SQL queries to analyze fraud metrics and prepare features for machine learning.

---

## Step 1: Create an S3 Query Results Bucket

Amazon Athena is serverless, but it requires an S3 bucket to save query execution history and results. We will use a dedicated folder inside our analytics bucket.

```bash
# Create the analytics bucket if it doesn't already exist (from general plan)
aws s3 mb s3://medicare-fraud-analytics-023413058557
```

---

## Step 2: Configure Athena Workgroup Query Location

You must configure Athena to use this S3 location for storing query results.

```bash
aws athena update-work-group --work-group primary --configuration-updates "ResultConfigurationUpdates={OutputLocation=s3://medicare-fraud-analytics-023413058557/athena-results/}"
```
*Explanation: This updates the default `Primary` workgroup in Athena to output all query results to our analytics S3 folder.*

---

## Step 3: Verify Partitions & Schema

Before running analytical queries, verify that the Glue Crawler successfully discovered all partition levels (`Year` and `State`) and mapped the table columns correctly.

### Check Table Information
```sql
SHOW CREATE TABLE medicare_db.inpatient_processed;
```

### Show Discovered Partitions
```sql
SHOW PARTITIONS medicare_db.inpatient_processed;
```

### Run a Sample Data Check (Verifies Partition Pruning works)
```sql
SELECT * 
FROM medicare_db.inpatient_processed
WHERE Year = '2008' AND State = '39'
LIMIT 10;
```
*Note: This query only scans S3 paths matching `Year=2008/State=39/`, minimizing S3 scan size and costs.*

---

## Step 4: Analytical SQL Queries for Fraud Detection

Run the following key analytical queries inside Athena (using the console or AWS CLI) to surface high-risk provider profiles:

### Query A: Top Providers by Total Reimbursement & Beneficiary Counts
Find providers billing exceptionally high total amounts with a low ratio of unique beneficiaries (potential billing inflation/phantom patients).

```sql
SELECT 
    provider,
    COUNT(DISTINCT claimid) AS total_claims,
    COUNT(DISTINCT beneid) AS unique_patients,
    SUM(inscclaimamtreimbursed) AS total_reimbursement,
    ROUND(SUM(inscclaimamtreimbursed) / COUNT(DISTINCT claimid), 2) AS avg_reimbursement_per_claim,
    ROUND(CAST(COUNT(DISTINCT claimid) AS DOUBLE) / COUNT(DISTINCT beneid), 2) AS claims_per_patient
FROM medicare_db.inpatient_processed
GROUP BY provider
HAVING COUNT(DISTINCT claimid) > 10
ORDER BY total_reimbursement DESC
LIMIT 50;
```

### Query B: Short-Stay / Same-Day Discharge with High Reimbursement
Look for high-value claims where patients were admitted and discharged on the same day or within 24 hours.

```sql
SELECT 
    provider,
    claimid,
    inscclaimamtreimbursed,
    deductibleamtpaid,
    admissiondt,
    dischargedt,
    date_diff('day', date_parse(admissiondt, '%Y%m%d'), date_parse(dischargedt, '%Y%m%d')) AS length_of_stay
FROM medicare_db.inpatient_processed
WHERE admissiondt = dischargedt  -- Same day stay
   OR date_diff('day', date_parse(admissiondt, '%Y%m%d'), date_parse(dischargedt, '%Y%m%d')) <= 1
ORDER BY inscclaimamtreimbursed DESC
LIMIT 50;
```

### Query C: Geographic Heatmap of Average Reimbursements
Compare average billing rates across different states to pinpoint regional fraud hotspots.

```sql
SELECT 
    state,
    COUNT(DISTINCT provider) AS total_active_providers,
    COUNT(DISTINCT claimid) AS total_claims,
    SUM(inscclaimamtreimbursed) AS total_reimbursement,
    ROUND(AVG(inscclaimamtreimbursed), 2) AS avg_reimbursement_per_claim
FROM medicare_db.inpatient_processed
GROUP BY state
ORDER BY avg_reimbursement_per_claim DESC;
```

---

## Step 5: Export ML Feature Store Table (Gold Zone)

To prepare for down-stream machine learning (SageMaker Isolation Forest in Phase 4), we can leverage Athena's `CREATE TABLE AS SELECT` (CTAS) pattern. This aggregates features at the **Provider level** and outputs clean parquet files straight into our Analytics Gold S3 zone.

```sql
CREATE TABLE medicare_db.provider_ml_features
WITH (
     format = 'PARQUET',
     external_location = 's3://medicare-fraud-analytics-023413058557/features/'
) AS 
WITH provider_stats AS (
    SELECT 
        provider,
        COUNT(DISTINCT claimid) AS total_claims,
        COUNT(DISTINCT beneid) AS unique_patients,
        SUM(inscclaimamtreimbursed) AS total_reimbursed,
        AVG(inscclaimamtreimbursed) AS avg_reimbursed,
        AVG(deductibleamtpaid) AS avg_deductible,
        AVG(date_diff('day', date_parse(admissiondt, '%Y%m%d'), date_parse(dischargedt, '%Y%m%d'))) AS avg_length_of_stay
    FROM medicare_db.inpatient_processed
    GROUP BY provider
)
SELECT 
    provider,
    total_claims,
    unique_patients,
    total_reimbursed,
    avg_reimbursed,
    avg_deductible,
    avg_length_of_stay,
    ROUND(CAST(total_claims AS DOUBLE) / unique_patients, 4) AS claims_per_patient_ratio
FROM provider_stats;
```

*Outcome: Athena compiles and dumps the aggregated features as standard Parquet files into `s3://medicare-fraud-analytics-023413058557/features/`, which is the exact S3 prefix our SageMaker Training Job reads from in Phase 4!*
