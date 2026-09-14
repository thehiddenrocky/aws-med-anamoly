set -e

echo "=== Medicare Fraud Detection Serverless Deployer ==="

# Configurations
SOURCE_FILE="etl-scripts/lambda-scoring.py"
PACKAGE_DIR="lambda_package"
ZIP_NAME="lambda_deploy.zip"
FUNCTION_NAME="medicare-provider-scorer"
ROLE_ARN="arn:aws:iam::023413058557:role/MedicareLambdaExecutionRole"
BUCKET="medicare-fraud-analytics-023413058557"

# 1. Clean previous build states
echo "1. Cleaning directory structures..."
rm -rf $PACKAGE_DIR $ZIP_NAME
mkdir -p $PACKAGE_DIR

# 2. Compile Linux x86_64 binaries directly from macOS
echo "2. Cross-compiling and downloading Linux x86_64 binaries (Omit Boto3)..."
pip install \
  --platform manylinux2014_x86_64 \
  --target=$PACKAGE_DIR \
  --implementation cp \
  --python-version 3.11 \
  --only-binary=:all: \
  scikit-learn joblib pandas "numpy<2.0"

# 3. Align Lambda Handler codebase
echo "3. Copying and mapping Lambda handler..."
cp $SOURCE_FILE $PACKAGE_DIR/lambda_function.py

# 4. Prune redundant assets to stay under 250MB limit
echo "4. Pruning redundant assets (tests, caches, metadata) to minimize size..."
cd $PACKAGE_DIR
find . -type d -name "tests" -exec rm -rf {} +
find . -type d -name "test" -exec rm -rf {} +
find . -type d -name "__pycache__" -exec rm -rf {} +
find . -type f -name "*.pyc" -delete
find . -type f -name "*.pyo" -delete
find . -type d -name "*.dist-info" -exec rm -rf {} +
find . -type d -name "*.egg-info" -exec rm -rf {} +
cd ..

# 5. Create deployment ZIP
echo "5. Generating optimized deployment ZIP archive..."
cd $PACKAGE_DIR
zip -r9 ../$ZIP_NAME .
cd ..

# 6. Upload package to S3 for fast, reliable function deployment
echo "6. Uploading deployment package to S3..."
command aws s3 cp $ZIP_NAME s3://$BUCKET/$ZIP_NAME

# 7. Create or update function in AWS Lambda
echo "7. Deploying package to AWS Lambda..."
EXISTS=$(command aws lambda get-function --function-name $FUNCTION_NAME 2>&1 || true)

if [[ $EXISTS == *"ResourceNotFoundException"* ]]; then
  echo "--> Creating new function: $FUNCTION_NAME"
  command aws lambda create-function \
    --function-name $FUNCTION_NAME \
    --runtime python3.11 \
    --role $ROLE_ARN \
    --handler lambda_function.lambda_handler \
    --code S3Bucket=$BUCKET,S3Key=$ZIP_NAME \
    --timeout 30 \
    --memory-size 1024 \
    --environment "Variables={BUCKET_NAME=$BUCKET,MODEL_KEY=models/model.joblib,SCALER_KEY=models/scaler.joblib,DYNAMODB_TABLE=medicare_provider_scores,BEDROCK_MODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0,BEDROCK_REGION=us-east-1}"
else
  echo "--> Updating existing function code..."
  command aws lambda update-function-code \
    --function-name $FUNCTION_NAME \
    --s3-bucket $BUCKET \
    --s3-key $ZIP_NAME
    
  echo "--> Updating configuration defaults..."
  command aws lambda update-function-configuration \
    --function-name $FUNCTION_NAME \
    --timeout 30 \
    --memory-size 1024 \
    --environment "Variables={BUCKET_NAME=$BUCKET,MODEL_KEY=models/model.joblib,SCALER_KEY=models/scaler.joblib,DYNAMODB_TABLE=medicare_provider_scores,BEDROCK_MODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0,BEDROCK_REGION=us-east-1}"
fi

echo "=== Deployment Completed Successfully! ==="
