# Medicare Inpatient Claims: Forensic Data Exploration & Feature Engineering Report

This report documents our analytical exploration, forensic healthcare audit insights, and advanced feature engineering methodology applied to the Medicare Inpatient Claims dataset. These queries and processes can be fully reproduced in Amazon Athena over the processed Silver/Gold data layers.

---

## 1. Executive Summary

Traditional fraud detection filters often focus solely on absolute dollar thresholds. However, this approach creates false positives by flagging legitimately sick patients who require prolonged hospitalizations. By conducting a detailed statistical audit, we discovered that normalizing reimbursement metrics against **length of stay (duration of admission)** acts as a highly effective filter to differentiate legitimate medical care from systemic fraud.

Through our analysis, we identified three major forensic patterns in the data:
* **Self-Approving Surgeons:** Attending physicians who act as their own operating physicians, self-authorizing massive surgeries with zero external oversight.
* **Ghost Billing & Footprint Inflation:** High-value single-visit physicians submitting claims across multiple geographical locations but never treating the same patient twice.
* **Rapid-Discharge Upcoding:** Artificially short inpatient stays (0 to 2 days) billed at maximum-reimbursement tiers.

---

## 2. Baseline Population Statistics

To understand our data, we first evaluated the global inpatient claim distribution:

* **Total Claims Analyzed:** 40,474
* **Average Claim Reimbursement:** $10,087.88
* **Standard Deviation of Claims:** $10,303.10
* **Maximum Claim Value:** $125,000.00 (Z-score = 11.15)
* **Statistical Distribution:** Because the standard deviation exceeds the average, the claims distribution is highly skewed with an extremely long, heavy right tail containing astronomical financial outliers.

### SQL Query: Retrieve Global Baseline Stats
```sql
SELECT 
    COUNT(*) AS total_claims,
    AVG(inscclaimamtreimbursed) AS avg_reimbursement,
    STDDEV(inscclaimamtreimbursed) AS stddev_reimbursement,
    MIN(inscclaimamtreimbursed) AS min_reimbursement,
    MAX(inscclaimamtreimbursed) AS max_reimbursement,
    (MAX(inscclaimamtreimbursed) - AVG(inscclaimamtreimbursed)) / STDDEV(inscclaimamtreimbursed) AS max_claim_z_score
FROM medicare_db.inpatient_processed;
```

---

## 3. Advanced Forensic Findings & Outliers

### Pattern A: Self-Approving Surgeons
We audited physicians who perform surgical procedures while simultaneously acting as the admitting/attending physician. This lack of dual-signature oversight is highly vulnerable to financial manipulation.
* **Provider `PRV55209` / Physician `PHY341560`:** 153 claims | **$1,909,000.00** total billed.
* **Provider `PRV51560` / Physician `PHY411541`:** 121 claims | **$1,401,000.00** total billed.
* **Provider `PRV52821` / Physician `PHY314410`:** 109 claims | **$1,261,000.00** total billed.

### Pattern B: Ghost Billing (The 3-Provider Footprint)
Physician `PHY359004` presents a striking billing signature:
* Associated with **3 distinct provider facilities**.
* Submitted **15 claims for 15 unique patients**, meaning they never treated or saw the same patient twice.
* Average claim is **$14,066.67** (Median **$9,000.00**), consistently shifting their entire billing profile into high-reimbursement brackets.

### Pattern C: Extreme Average-to-Median Skew
Physician `PHY335080` has an average claim size of **$12,758.62** but a median of only **$7,000.00**. This statistical profile indicates that while they bill "standard" outpatient/inpatient rates most of the time, their average is heavily warped upwards by a few astronomical, high-dollar inpatient claims.

---

## 4. The Feature Engineering Breakthrough: Daily Reimbursement Rates

A major data science challenge in medical claims is distinguishing long-term care from fraud. 
Compare these two claims:
* **Claim A (Legitimate Stay):** 35-day hospitalization costing **$93,000.00** $\rightarrow$ **$2,657.14 per day** (reasonable for room, board, and severe care).
* **Claim B (Highly Suspicious Stay):** 7-day hospitalization costing **$92,000.00** $\rightarrow$ **$11,500.00 per day** (extreme, unaligned markup).

To expose these, we engineered a new normalized metric:
$$\text{Daily Reimbursement Rate} = \frac{\text{inscclaimamtreimbursed}}{\text{length\_of\_stay} + 1.0}$$
*Note: We add 1.0 to the length of stay to prevent division-by-zero errors for 0-day stays (same-day admit/discharge).*

---

## 5. Identifying the Top Anomaly Cohort

We compiled these metrics into a permanent ML-ready feature table (`provider_ml_features`) using a **CTAS (Create Table As Select)** query in Athena. This table aggregates features at the provider level and exposes the following critical anomalies:

1. **`PRV53033` (The Patient Churner) — High Risk**
   * **Total Claims:** 2 | **Unique Patients:** 1 | **Claims/Patient Ratio:** 2.0
   * **Avg Stay:** 3.5 days | **Avg Daily Rate:** $18,392.86 | **Max Daily Rate:** $36,500.00
   * *Forensic Interpretation:* This provider repeatedly admits and discharges the exact same patient for brief durations while billing up to **$36.5k per day**, likely to trigger multiple inpatient deductibles and maximize payout.

2. **`PRV52627` (The Rapid-Discharge Upcoder) — High Risk**
   * **Total Claims:** 3 | **Unique Patients:** 3 | **Avg Stay:** 1.67 days
   * **Avg Daily Rate:** $13,333.33 | **Max Daily Rate:** $37,000.00
   * *Forensic Interpretation:* Patients are discharged after less than 2 days on average, yet the provider regularly bills maximum tier-1 inpatient rates, resulting in a daily peak rate of **$37k**.

3. **`PRV53471` (The 0-Day Outlier) — Medium-High Risk**
   * **Total Claims:** 1 | **Avg Stay:** 0.0 days | **Daily Rate:** $19,000.00
   * *Forensic Interpretation:* This provider billed a full inpatient reimbursement of **$19,000.00** for a same-day discharge. Under federal guidelines, inpatient stays generally require a "two-midnight" stay; same-day claims are high-priority recovery targets.

---

## 6. Complete Replication Query Suite

Run these queries in Amazon Athena to reproduce the entire data exploration and generate the feature store.

### Query 1: Identify Self-Approving Surgeons
```sql
SELECT 
    provider,
    attendingphysician,
    operatingphysician,
    COUNT(*) AS self_approved_claims,
    SUM(inscclaimamtreimbursed) AS total_reimbursed
FROM medicare_db.inpatient_processed
WHERE attendingphysician = operatingphysician
  AND attendingphysician IS NOT NULL
GROUP BY provider, attendingphysician, operatingphysician
HAVING COUNT(*) > 10
ORDER BY total_reimbursed DESC;
```

### Query 2: Audit Individual Claims for Flagged Physicians (PHY359004 & PHY335080)
```sql
SELECT 
    claimid,
    provider,
    beneid,
    inscclaimamtreimbursed,
    deductibleamtpaid,
    admissiondt,
    dischargedt,
    date_diff('day', admissiondt, dischargedt) AS length_of_stay,
    operatingphysician
FROM medicare_db.inpatient_processed
WHERE attendingphysician IN ('PHY359004', 'PHY335080')
ORDER BY attendingphysician, inscclaimamtreimbursed DESC;
```

### Query 3: Create the Feature Store with Normalized Daily Rates (CTAS)
*Run this query to build the analytical dataset containing your engineered daily metrics in the gold S3 prefix.*
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

### Query 4: View the Top 10 Most Suspicious Providers
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
ORDER BY high_daily_claims_ratio DESC, max_daily_reimbursement DESC
LIMIT 10;
```
