Step 1: Create a New IAM User
  To create a dedicated developer user in your AWS account, use the following command (replace developer-user
  with your preferred name):

   1 aws iam create-user --user-name developer-user

  Step 2: Attach a Policy (Optional but Recommended)
  A newly created user has zero permissions by default. You need to attach a policy so the user can interact
  with services like S3, DynamoDB, or Lambda. For example, to give this user full S3 permissions:

   1 aws iam attach-user-policy \
   2     --user-name developer-user \
   3     --policy-arn arn:aws:iam::aws:policy/AmazonS3FullAccess

  Step 3: Generate the Access Key and Secret Key
  Now, generate the programmatic credentials for this user:

   1 aws iam create-access-key --user-name developer-user
