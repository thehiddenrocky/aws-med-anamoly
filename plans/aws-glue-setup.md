Step 1: Create the IAM Role for AWS Glue
  AWS Glue requires an IAM role with permissions to access S3 (read the raw bronze data, write
  the processed silver data) and log execution details to CloudWatch.

   1. Create a Trust Policy (glue-trust-policy.json) allowing the Glue service to assume this
      role:

    1 {
    2   "Version": "2012-10-17",
    3   "Statement": [
    4     {
    5       "Effect": "Allow",
    6       "Principal": {
    7         "Service": "glue.amazonaws.com"
    8       },
    9       "Action": "sts:AssumeRole"
   10     }
   11   ]
   12 }

   2. Create the Role:

   1 aws iam create-role \
   2     --role-name MedicareGlueETLRole \
   3     --assume-role-policy-document file://glue-trust-policy.json
   * Explanation: Creates an IAM role named MedicareGlueETLRole that only the AWS Glue service
     can assume.

   3. Attach the AWS Glue Managed Service Policy:

   1 aws iam attach-role-policy \
   2     --role-name MedicareGlueETLRole \
   3     --policy-arn arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole
   * Explanation: Attaches AWSGlueServiceRole, which contains necessary permissions for Glue
     to create logs in CloudWatch, access the Glue Catalog, and communicate within its VPC.

   4. Attach S3 Access Policy:
  Create a custom policy (glue-s3-policy.json) for least-privilege S3 bucket access:

    1 {
    2   "Version": "2012-10-17",
    3   "Statement": [
    4     {
    5       "Effect": "Allow",
    6       "Action": [
    7         "s3:GetObject",
    8         "s3:PutObject",
    9         "s3:DeleteObject",
   10         "s3:ListBucket"
   11       ],
   12       "Resource": [
   13         "arn:aws:s3:::medicare-fraud-raw",
   14         "arn:aws:s3:::medicare-fraud-raw/*",
   15         "arn:aws:s3:::medicare-fraud-processed",
   16         "arn:aws:s3:::medicare-fraud-processed/*"
   17       ]
   18     }
   19   ]
   20 }
  And attach it to your role:

   1 aws iam put-role-policy \
   2     --role-name MedicareGlueETLRole \
   3     --policy-name S3AccessForMedicareETL \
   4     --policy-document file://glue-s3-policy.json
   * Explanation: Grants read access to the raw bucket (bronze) and read/write access to the
     processed bucket (silver).

  ---

  Step 2: Upload the PySpark Script to S3
  Save the PySpark script defined in Phase 2 of @plans/general-plan.md to a local file named
  medicare_etl.py and upload it to an administrative path in S3.

   1 aws s3 cp medicare_etl.py s3://medicare-fraud-raw/scripts/medicare_etl.py
   * Explanation: Copies your local script into S3 so the serverless Glue Spark engines can
     fetch and run it.

  ---

  Step 3: Create the AWS Glue Job
  Register the Spark job details with AWS Glue using create-job.

   1 aws glue create-job \
   2     --name medicare-etl-job \
   3     --role MedicareGlueETLRole \
   4     --command '{"Name": "glueetl", "ScriptLocation":
     "s3://medicare-fraud-raw/scripts/medicare_etl.py", "PythonVersion": "3"}' \
   5     --default-arguments '{"--job-language": "python", "--TempDir":
     "s3://medicare-fraud-raw/temp/"}' \
   6     --glue-version "4.0" \
   7     --worker-type "G.1X" \
   8     --number-of-workers 2
   * Explanation of Parameters:
     * --name: The name of your Spark ETL pipeline.
     * --role: The name of the IAM role you created in Step 1.
     * --command: Sets the execution command. Name: "glueetl" tells Glue to run a PySpark job.
       ScriptLocation points to your script in S3.
     * --default-arguments: Key-value configuration parameters passed to Spark. --TempDir
       defines where Spark writes intermediate map-reduce results.
     * --glue-version "4.0": Configures the job to run under AWS Glue 4.0 (which defaults to
       Spark 3.3.0 and Python 3.10).
     * --worker-type "G.1X": Standard worker size containing 4 vCPUs, 16 GB memory, and 64 GB
       disk space.
     * --number-of-workers 2: Spawns 2 executor instances (1 master, 1 worker). This is
       cost-efficient and sufficient for development/learning datasets.

  ---

  Step 4: Run and Monitor the Job

   1. Trigger the Job:

   1 aws glue start-job-run --job-name medicare-etl-job
   * Explanation: Starts execution of the Spark cluster asynchronously. It will return a JSON
     payload with a JobRunId.

   2. Check Run Status:
  Replace <YOUR_JOB_RUN_ID> with the ID returned by the previous command:

   1 aws glue get-job-run --job-name medicare-etl-job --run-id <YOUR_JOB_RUN_ID>
   * Explanation: Queries execution state. Look inside the JSON output under
     JobRun.JobRunState (e.g. STARTING, RUNNING, SUCCEEDED, or FAILED). Check CloudWatch Logs
     under the /aws-glue/jobs/output group for detailed print outputs.
▄▄
