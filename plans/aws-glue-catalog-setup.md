# AWS Glue Data Catalog Setup Plan

This plan details the AWS CLI steps required to register the processed dataset schema in the AWS Glue Data Catalog and make it queryable via Amazon Athena. This corresponds to the cataloging step in Phase 2 of the `general-plan.md`.

## Step 1: Create the AWS Glue Database

Before we can crawl the data, we need a Glue Database to act as a logical container for our tables.

```bash
aws glue create-database \
    --database-input '{"Name": "medicare_db", "Description": "Database for Medicare Fraud Detection processed data"}'
```
*Explanation: This creates a Glue Database named `medicare_db` which Athena will query.*

## Step 2: Create the AWS Glue Crawler

We will create a crawler that scans the processed S3 zone (silver tier) to automatically infer the schema (including our `Year` and `State` partitions) and register the table in the `medicare_db`. 

**Note:** We will use the existing `MedicareGlueETLRole` created in earlier steps, as it already has the required S3 permissions and AWS Glue service trust policies.

```bash
aws glue create-crawler \
    --name medicare-processed-crawler \
    --role MedicareGlueETLRole \
    --database-name medicare_db \
    --targets '{"S3Targets": [{"Path": "s3://medicare-fraud-processed-023413058557/inpatient_processed/"}]}'
```
*Explanation: The crawler targets the `inpatient_processed` directory. Upon execution, it will generate an `inpatient_processed` table within `medicare_db`.*

## Step 3: Start the Crawler

Trigger the crawler to begin scanning the Parquet files and discovering the partitions.

```bash
aws glue start-crawler --name medicare-processed-crawler
```
*Explanation: This asynchronously starts the crawler. It will take a minute or two to complete.*

## Step 4: Monitor Crawler Status

You can monitor the status of the crawler to ensure it successfully completes.

```bash
aws glue get-crawler --name medicare-processed-crawler --query "Crawler.State" --output text
```
*Explanation: Wait for the state to return to `READY`. Once it is `READY`, the table and partitions are registered and available for querying in Athena.*
