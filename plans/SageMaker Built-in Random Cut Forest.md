# Plan 1: SageMaker Built-in Random Cut Forest (RCF)

This plan leverages Amazon SageMaker's native, highly optimized Random Cut Forest (RCF) anomaly detection algorithm. Under the hood, RCF is an unsupervised algorithm that calculates an anomaly score (attribution score) for each data point based on its impact on the volume of a bounding box of trees.

---

## 🏗️ Architectural Overview & Workflow
1. **Data Prep**: Load Gold Parquet features, extract numerical columns, save as headerless/indexless CSV, and upload to S3.
2. **Model Training**: Use a SageMaker training job with the official RCF container to build the forest ensemble.
3. **Batch Scoring**: Create a model endpoint configuration and run a Batch Transform job to generate scores for all provider profiles.
4. **Post-Processing & Flagging**: Align raw scores back to Provider IDs and apply a 95th percentile threshold to flag anomalies.

---

## 🛠️ Step-by-Step AWS CLI Implementation Guide

To maximize your learning of the raw AWS APIs, follow these step-by-step AWS CLI commands. 

### Environment Constants
For these commands, we use the following pre-configured parameters:
* **S3 Bucket**: `medicare-fraud-analytics-023413058557`
* **AWS Region**: `us-east-1`
* **RCF Docker Image (us-east-1)**: `382416822647.dkr.ecr.us-east-1.amazonaws.com/randomcutforest:1`
* **IAM Role ARN**: `arn:aws:iam::023413058557:role/MedicareGlueETLRole`

---

### Step 1: Prepare and Upload Training Data

#### 1. Generate local CSV
First, run the python script to extract the 7 numerical features without a header or index, and preserve the provider ID row mapping:
```bash
python etl-scripts/prepare_rcf_data.py
```

#### 2. Upload to S3 Raw Training Prefix
```bash
aws s3 cp rcf_train.csv s3://medicare-fraud-analytics-023413058557/rcf/train/rcf_train.csv
```

#### 🔍 Step 1 Verification Command
Verify that the file is successfully uploaded and has the correct size:
```bash
aws s3 ls s3://medicare-fraud-analytics-023413058557/rcf/train/ --human-readable
```

---

### Step 2: Trigger SageMaker RCF Training Job

Initiate a serverless training job using the built-in RCF algorithm. This command creates a standard `ml.m5.large` node, pulls the official container, trains 200 trees, and saves the resulting model tarball.

```bash
aws sagemaker create-training-job \
    --training-job-name medicare-rcf-training-job \
    --algorithm-specification TrainingImage=382416822647.dkr.ecr.us-east-1.amazonaws.com/randomcutforest:1,TrainingInputMode=File \
    --role-arn arn:aws:iam::023413058557:role/MedicareGlueETLRole \
    --input-data-config '[{"ChannelName":"train","DataSource":{"S3DataSource":{"S3DataType":"S3Prefix","S3Uri":"s3://medicare-fraud-analytics-023413058557/rcf/train/rcf_train.csv","S3DataDistributionType":"FullyReplicated"}},"ContentType":"text/csv"}]' \
    --output-data-config S3OutputPath=s3://medicare-fraud-analytics-023413058557/rcf/output/ \
    --resource-config InstanceType=ml.m5.large,InstanceCount=1,VolumeSizeInGB=10 \
    --stopping-condition MaxRuntimeInSeconds=3600 \
    --hyper-parameters num_trees=200,num_samples_per_tree=256
```

#### 🔍 Step 2 Verification Command
Query the current status of the training job. Wait until status returns `Completed`:
```bash
aws sagemaker describe-training-job \
    --training-job-name medicare-rcf-training-job \
    --query "TrainingJobStatus" \
    --output text
```

If you want to view the output location of the trained model artifact (`model.tar.gz`), run:
```bash
aws sagemaker describe-training-job \
    --training-job-name medicare-rcf-training-job \
    --query "ModelArtifacts.S3ModelArtifacts" \
    --output text
```

---

### Step 3: Register the Model

Register a logical SageMaker Model resource referencing the trained container image and model artifact:

```bash
aws sagemaker create-model \
    --model-name medicare-rcf-model \
    --primary-container Image=382416822647.dkr.ecr.us-east-1.amazonaws.com/randomcutforest:1,ModelDataUrl=s3://medicare-fraud-analytics-023413058557/rcf/output/medicare-rcf-training-job/output/model.tar.gz \
    --execution-role-arn arn:aws:iam::023413058557:role/MedicareGlueETLRole
```

#### 🔍 Step 3 Verification Command
Verify the model was created correctly in the registry:
```bash
aws sagemaker describe-model \
    --model-name medicare-rcf-model \
    --query "PrimaryContainer.ModelDataUrl" \
    --output text
```

---

### Step 4: Run Batch Transform (Inference scoring)

Since we want to generate anomaly scores for all 2,092 providers without keeping a persistent server running, we deploy a **Batch Transform** job. This takes the model we registered in Step 3 and applies it directly to our training CSV:

```bash
aws sagemaker create-transform-job \
    --transform-job-name medicare-rcf-transform-job \
    --model-name medicare-rcf-model \
    --transform-input DataSource={S3DataSource={S3DataType=S3Prefix,S3Uri=s3://medicare-fraud-analytics-023413058557/rcf/train/rcf_train.csv}},ContentType=text/csv,SplitType=Line \
    --transform-output S3OutputPath=s3://medicare-fraud-analytics-023413058557/rcf/predictions/,Accept=text/csv \
    --transform-resources InstanceType=ml.m5.large,InstanceCount=1
```

#### 🔍 Step 4 Verification Command
Check the status of the transform job. Wait until status returns `Completed`:
```bash
aws sagemaker describe-transform-job \
    --transform-job-name medicare-rcf-transform-job \
    --query "TransformJobStatus" \
    --output text
```

---

### Step 5: Download and Post-Process Results

#### 1. Download predictions from S3
Once the Batch Transform job is complete, download the generated score CSV:
```bash
aws s3 cp s3://medicare-fraud-analytics-023413058557/rcf/predictions/rcf_train.csv.out local_scores.csv
```

#### 2. Local Score Evaluation (Learning Interpretation)
Since RCF outputs raw positive scores where **higher scores indicate stronger anomaly signals**, we can calculate the 95th percentile threshold (for our 5% contamination rate) and map them back to provider IDs by executing a simple terminal evaluation:

```bash
python -c "
import pandas as pd
import numpy as np

# Load provider mappings and RCF scores
providers = pd.read_csv('provider_mapping.csv')
scores = pd.read_csv('local_scores.csv', header=None, names=['score'])
results = pd.concat([providers, scores], axis=1)

# Isolate top 5% anomalies
threshold = np.percentile(results['score'], 95)
results['is_anomaly'] = results['score'] >= threshold

print(f'Anomaly Threshold (95th %): {threshold:.4f}')
print(f'Anomalies flagged: {results[\"is_anomaly\"].sum()}')

print('\n--- TOP 5 ANOMALOUS PROVIDERS ---')
print(results.sort_values(by='score', ascending=False).head(5).to_string(index=False))

print('\n--- AUDIT OF TARGET ANOMALIES ---')
targets = ['PRV53033', 'PRV52627', 'PRV53471']
print(results[results['provider'].isin(targets)].to_string(index=False))
"
```

This completes the entire AWS CLI SageMaker Random Cut Forest lifecycle. You can compare these outcomes directly to your local scikit-learn Isolation Forest predictions!
