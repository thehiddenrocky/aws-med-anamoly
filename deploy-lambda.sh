set -e

echo "=== Medicare Fraud Detection Serverless Deployer ==="

# Define configurations
LAMBDA_DIR="lambda-scoring"
PACKAGE_DIR="lambda_package"
ZIP_NAME="lambda_deploy.zip"
FUNCTION_NAME="medicare-provider-scorer"
ROLE_ARN="arn:aws:iam::023413058557:role/MedicareLambdaExecutionRole"
BUCKET="medicare-fraud-analytics-023413058557"

# Clean previous builds
echo "1. Cleaning directory structures..."
rm -rf $PACKAGE_DIR $ZIP_NAME
mkdir -p $PACKAGE_DIR

# Pull Linux wheels direct from PyPI on macOS (OMITTING boto3)
echo "2. Cross-compiling and downloading Linux x86_64 binaries..."
pip install \
  --platform manylinux2014_x86_64 \
  --target=$PACKAGE_DIR \
  --implementation cp \
  --python-version 3.11 \
  --only-binary=:all: \
  scikit-learn joblib pandas "numpy<2.0"

# Copy scoring codebase into package
echo "3. Copying Lambda handlers..."
cp $LAMBDA_DIR/lambda_function.py $PACKAGE_DIR/

# Prune tests, caches, and metadata to stay under Lambda 250MB unzipped limit
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

# Create ZIP archive
echo "5. Generating optimized deployment ZIP file..."
cd $PACKAGE_DIR
zip -r9 ../$ZIP_NAME .
cd ..

# Deploying to AWS Lambda
echo "6. Uploading deployment package to S3..."
command aws s3 cp $ZIP_NAME s3://$BUCKET/$ZIP_NAME

echo "7. Deploying package to AWS Lambda from S3..."
# Check if function already exists
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
    --environment "Variables={BUCKET_NAME=$BUCKET,MODEL_KEY=models/model.joblib,SCALER_KEY=models/scaler.joblib}"
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
    --environment "Variables={BUCKET_NAME=$BUCKET,MODEL_KEY=models/model.joblib,SCALER_KEY=models/scaler.joblib}"
fi

echo "=== Deployment Completed Successfully! ==="
